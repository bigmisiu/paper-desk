"""Daily bars: Polygon grouped aggregates if keyed, else Yahoo Chart v8.

Never raises because POLYGON_API_KEY is missing. Logs the source actually used.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from desk.config import LOOKBACK, HTTP_TIMEOUT, polygon_api_key, YAHOO_UA
from desk import http

log = logging.getLogger("desk.bars")

ET = ZoneInfo("America/New_York")
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_CHART_2 = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_CRUMB = "https://query1.finance.yahoo.com/v1/test/getcrumb"
POLYGON_GROUPED = (
    "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
)

Bar = dict[str, Any]


class BarsError(RuntimeError):
    pass


def _upsert_bars(conn: sqlite3.Connection, rows: list[Bar]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO bars(ticker, date, open, high, low, close, volume, source)
        VALUES (:ticker, :date, :open, :high, :low, :close, :volume, :source)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, source=excluded.source
        """,
        rows,
    )
    conn.commit()


def bars_for(conn: sqlite3.Connection, ticker: str) -> list[Bar]:
    rows = conn.execute(
        """
        SELECT ticker, date, open, high, low, close, volume, source
        FROM bars WHERE ticker = ? ORDER BY date
        """,
        (ticker,),
    ).fetchall()
    return [dict(r) for r in rows]


def _session_dates(n_calendar: int = 40) -> list[str]:
    """Recent NY-calendar dates, skipping weekends. Holidays come back empty."""
    today = datetime.now(ET).date()
    # Before ~18:00 ET, today's regular session is not closed — don't ask for it.
    now = datetime.now(ET)
    cursor = today if now.hour >= 18 else today - timedelta(days=1)
    out: list[str] = []
    while len(out) < n_calendar:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return out


def _fetch_polygon_grouped(day: str, api_key: str) -> list[Bar]:
    url = POLYGON_GROUPED.format(day=day)
    resp = http.get(
        url,
        params={"adjusted": "true", "apiKey": api_key},
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code in (401, 403):
        raise BarsError(f"polygon grouped {resp.status_code} (plan/key)")
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise BarsError(f"polygon grouped HTTP {resp.status_code} for {day}")
    payload = resp.json()
    results = payload.get("results") or []
    rows: list[Bar] = []
    for item in results:
        sym = (item.get("T") or "").upper()
        if not sym:
            continue
        ts = item.get("t")
        if ts:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(ET).date()
            day_s = d.isoformat()
        else:
            day_s = day
        vol = item.get("v") or 0
        close = item.get("c")
        if close is None:
            continue
        rows.append(
            {
                "ticker": sym,
                "date": day_s,
                "open": item.get("o") or close,
                "high": item.get("h") or close,
                "low": item.get("l") or close,
                "close": close,
                "volume": int(vol),
                "source": "polygon",
            }
        )
    return rows


def load_polygon(conn: sqlite3.Connection, constituents: list[dict[str, str]], api_key: str) -> str:
    want = {}
    for c in constituents:
        want[c["polygon_ticker"].upper()] = c["ticker"]
        want[c["ticker"].upper()] = c["ticker"]
    dates = _session_dates(LOOKBACK + 15)
    got_days = 0
    for day in dates:
        try:
            raw = _fetch_polygon_grouped(day, api_key)
        except BarsError:
            raise
        except Exception as exc:
            log.warning("polygon grouped %s failed: %s", day, exc)
            continue
        if not raw:
            continue
        mapped: list[Bar] = []
        for bar in raw:
            canon = want.get(bar["ticker"])
            if not canon:
                continue
            bar = dict(bar)
            bar["ticker"] = canon
            mapped.append(bar)
        _upsert_bars(conn, mapped)
        if mapped:
            got_days += 1
            log.info("polygon %s: %d universe bars", day, len(mapped))
        if got_days >= LOOKBACK + 5:
            break
        time.sleep(0.15)
    if got_days < 5:
        raise BarsError(f"polygon grouped returned only {got_days} sessions")
    log.info("bars source: polygon (%d sessions)", got_days)
    return "polygon"


class _YahooSession:
    def __init__(self) -> None:
        import requests

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": YAHOO_UA,
                "Accept": "application/json,text/plain,*/*",
            }
        )
        self.crumb: str | None = None
        self.gate_until = 0.0
        self.lock = threading.Lock()

    def punish(self, seconds: float) -> None:
        with self.lock:
            self.gate_until = max(self.gate_until, time.time() + seconds)

    def wait_gate(self) -> None:
        while True:
            extra = self.gate_until - time.time()
            if extra <= 0:
                return
            time.sleep(min(extra, 1.0))

    def ensure_crumb(self) -> None:
        if self.crumb:
            return
        for url in ("https://finance.yahoo.com/", "https://fc.yahoo.com"):
            try:
                self.session.get(url, timeout=15)
            except Exception as exc:
                log.info("yahoo cookie warmup %s: %s", url, exc)
        try:
            r = self.session.get(YAHOO_CRUMB, timeout=15)
            text = (r.text or "").strip()
            if r.status_code == 200 and text and "<" not in text and len(text) < 80:
                self.crumb = text
                log.info("yahoo crumb set")
            else:
                log.info("yahoo crumb status=%s", r.status_code)
        except Exception as exc:
            log.info("yahoo crumb skipped: %s", exc)


def _parse_yahoo_chart(payload: dict[str, Any], canon: str) -> list[Bar]:
    chart = (payload or {}).get("chart") or {}
    err = chart.get("error")
    if err:
        raise BarsError(str(err))
    results = chart.get("result") or []
    if not results:
        return []
    node = results[0]
    ts = node.get("timestamp") or []
    quote = (node.get("indicators") or {}).get("quote") or [{}]
    q = quote[0] if quote else {}
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    vols = q.get("volume") or []
    rows: list[Bar] = []
    for i, t in enumerate(ts):
        try:
            close = closes[i]
            vol = vols[i]
        except IndexError:
            continue
        if close is None or vol is None:
            continue
        d = datetime.fromtimestamp(int(t), tz=timezone.utc).astimezone(ET).date().isoformat()
        o = opens[i] if i < len(opens) and opens[i] is not None else close
        h = highs[i] if i < len(highs) and highs[i] is not None else close
        l = lows[i] if i < len(lows) and lows[i] is not None else close
        rows.append(
            {
                "ticker": canon,
                "date": d,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(close),
                "volume": int(vol),
                "source": "yahoo",
            }
        )
    return rows


def _yahoo_one(ys: _YahooSession, yahoo_sym: str, canon: str) -> list[Bar]:
    ys.wait_gate()
    params: dict[str, Any] = {
        "interval": "1d",
        "range": "2mo",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    if ys.crumb:
        params["crumb"] = ys.crumb
    last_status = None
    for base in (YAHOO_CHART, YAHOO_CHART_2):
        url = base.format(symbol=yahoo_sym)
        resp = http.get(url, params=params, session=ys.session, timeout=20, retries=2)
        last_status = resp.status_code
        if resp.status_code == 429:
            ys.punish(15)
            continue
        if resp.status_code in (401, 403):
            ys.ensure_crumb()
            if ys.crumb:
                params["crumb"] = ys.crumb
            continue
        if resp.status_code != 200:
            continue
        try:
            return _parse_yahoo_chart(resp.json(), canon)
        except Exception as exc:
            log.warning("yahoo parse %s: %s", yahoo_sym, exc)
            return []
    log.warning("yahoo miss %s status=%s", yahoo_sym, last_status)
    return []


def load_yahoo(conn: sqlite3.Connection, constituents: list[dict[str, str]]) -> str:
    ys = _YahooSession()
    ys.ensure_crumb()
    ok = 0
    fail = 0
    stop = threading.Event()

    def work(c: dict[str, str]) -> tuple[str, list[Bar]]:
        if stop.is_set():
            return c["ticker"], []
        return c["ticker"], _yahoo_one(ys, c["yahoo_ticker"], c["ticker"])

    # Modest parallelism; Yahoo 429s the whole process via RateGate.
    workers = 4
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(work, c) for c in constituents]
        for fut in as_completed(futs):
            ticker, rows = fut.result()
            if stop.is_set() and not rows:
                continue
            if rows:
                _upsert_bars(conn, rows)
                ok += 1
            else:
                fail += 1
            if (ok + fail) % 50 == 0:
                log.info("yahoo progress %d ok / %d miss", ok, fail)
            if ok == 0 and fail >= 12:
                stop.set()
                log.warning("yahoo circuit open after %d consecutive misses (likely 429)", fail)
                break
    log.info("bars source: yahoo (ok=%d miss=%d)", ok, fail)
    if ok < 100:
        tickers = [c["ticker"] for c in constituents]
        cached = coverage(conn, tickers)
        if cached >= 100:
            log.warning("yahoo fetch thin; using sqlite cache (%d names)", cached)
            return "yahoo-cache"
        raise BarsError(f"Yahoo Chart v8 usable bars for only {ok} names (cache={cached})")
    return "yahoo"


def coverage(conn: sqlite3.Connection, tickers: list[str]) -> int:
    if not tickers:
        return 0
    q = ",".join("?" * len(tickers))
    row = conn.execute(
        f"SELECT COUNT(DISTINCT ticker) AS n FROM bars WHERE ticker IN ({q})",
        tickers,
    ).fetchone()
    return int(row["n"] if row else 0)


def load_bars(conn: sqlite3.Connection, constituents: list[dict[str, str]]) -> str:
    """Fill sqlite bars. Returns 'polygon' or 'yahoo'."""
    key = polygon_api_key()
    if key:
        try:
            return load_polygon(conn, constituents, key)
        except Exception as exc:
            log.warning("polygon unavailable (%s); falling back to Yahoo Chart v8", exc)
    else:
        log.info("POLYGON_API_KEY unset; using Yahoo Chart v8")
    return load_yahoo(conn, constituents)


def last_session(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM bars").fetchone()
    return None if row is None else row["d"]
