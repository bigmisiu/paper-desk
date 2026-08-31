"""Two-stage scan: cheap volume screen, then hydrate top N. Write notes markdown."""

from __future__ import annotations

import logging
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo
from xml.etree.ElementTree import ParseError

from desk.bars import BarsError, bars_for, last_session, load_bars
from desk.config import (
    BLOTTER_HEADER,
    BLOTTER_PATH,
    LOOKBACK,
    MIN_DOLLAR_VOL,
    NOTES_DIR,
    TOP_N,
    YAHOO_UA,
    connect_db,
    polygon_api_key,
)
from desk.constituents import ConstituentsError, load_constituents
from desk.edgar import fetch_daily_indexes, filter_ciks, hydrate_filings
from desk import http
from desk.notify import post_scan

log = logging.getLogger("desk")

PT = ZoneInfo("America/Los_Angeles")
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
YAHOO_RSS_ALT = "https://finance.yahoo.com/rss/headline?s={symbol}"

REL_VOL_FLAG = 1.5
GAP_PCT = 0.015


def _today_pt() -> date:
    return datetime.now(PT).date()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def ensure_blotter() -> None:
    path = BLOTTER_PATH
    if path.is_file() and path.stat().st_size > 0:
        return
    path.write_text(BLOTTER_HEADER + "\n", encoding="utf-8")
    log.info("wrote blotter header %s", path)


def write_notes(day, body: str) -> None:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTES_DIR / f"{day.isoformat()}.md"
    path.write_text(body, encoding="utf-8")
    log.info("wrote %s", path)
    post_scan(day, body)


def data_fail(day, reason: str, detail: str) -> int:
    body = (
        f"# DATA_FAIL — {day.isoformat()}\n\n"
        f"**Reason:** {reason}\n\n"
        f"{detail.strip()}\n\n"
        "Scanner exited 1. Fix the source (Wikipedia table / Yahoo Chart / Polygon) "
        "or wait out a rate limit. Cached sqlite is in data/scan.sqlite if any.\n"
    )
    write_notes(day, body)
    log.error("DATA_FAIL %s: %s", reason, detail)
    return 1


def _rel_vol(volumes: list[int]) -> tuple[int, float, float]:
    """last volume, avg of prior LOOKBACK, ratio. Need LOOKBACK+1 bars."""
    if len(volumes) < LOOKBACK + 1:
        last = volumes[-1] if volumes else 0
        return last, 0.0, 0.0
    last = volumes[-1]
    prior = volumes[-(LOOKBACK + 1) : -1]
    avg = sum(prior) / float(LOOKBACK)
    ratio = (last / avg) if avg else 0.0
    return last, avg, ratio


def score_universe(
    constituents: list[dict[str, str]],
    conn,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for c in constituents:
        hist = bars_for(conn, c["ticker"])
        if len(hist) < LOOKBACK + 1:
            continue
        last = hist[-1]
        prev = hist[-2]
        close = float(last["close"])
        prev_close = float(prev["close"])
        vol = int(last["volume"] or 0)
        dollar = close * vol
        if dollar < MIN_DOLLAR_VOL:
            continue
        vols = [int(b["volume"] or 0) for b in hist]
        last_vol, avg_vol, ratio = _rel_vol(vols)
        ret = (close / prev_close - 1.0) if prev_close else 0.0
        open_px = float(last["open"] or close)
        gap = (open_px / prev_close - 1.0) if prev_close else 0.0
        ranked.append(
            {
                **c,
                "last": close,
                "prev_close": prev_close,
                "open": open_px,
                "high": float(last["high"] or close),
                "low": float(last["low"] or close),
                "volume": last_vol,
                "avg_volume": avg_vol,
                "rel_vol": ratio,
                "ret_1d": ret,
                "gap": gap,
                "dollar_vol": dollar,
                "bar_date": last["date"],
                "bar_source": last["source"],
            }
        )
    ranked.sort(
        key=lambda r: (r["rel_vol"], abs(r["ret_1d"]), r["dollar_vol"]),
        reverse=True,
    )
    return ranked


def _rss_items(xml_text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ParseError:
        return []
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or item.findtext("pubdate") or "").strip()
        if not title:
            continue
        items.append({"title": title, "url": link, "pub": pub})
    return items


def fetch_headline(yahoo_symbol: str) -> dict[str, str] | None:
    headers = {"User-Agent": YAHOO_UA, "Accept": "application/rss+xml,application/xml,text/xml"}
    for url in (
        YAHOO_RSS.format(symbol=yahoo_symbol),
        YAHOO_RSS_ALT.format(symbol=yahoo_symbol),
    ):
        try:
            resp = http.get(url, headers=headers, timeout=15, retries=2)
        except Exception as exc:
            log.info("rss %s: %s", yahoo_symbol, exc)
            continue
        if resp.status_code != 200:
            continue
        items = _rss_items(resp.text)
        if items:
            return items[0]
    return None


def _parse_pub(pub: str):
    if not pub:
        return None
    from dateutil import parser as dateparser

    try:
        dt = dateparser.parse(pub)
        return dt.date()
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_filed(filed: str):
    s = (filed or "").replace("-", "")
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def gap_flag(row: dict[str, Any], event_date) -> str:
    """GAP-AND-GONE vs still-dated. Facts, not a trade call."""
    gap = float(row.get("gap") or 0)
    ret = float(row.get("ret_1d") or 0)
    bar_s = row.get("bar_date")
    try:
        bar_d = datetime.strptime(bar_s, "%Y-%m-%d").date() if bar_s else None
    except ValueError:
        bar_d = None

    if abs(gap) >= GAP_PCT:
        # still extended vs prev close → gone; back toward prev close → filled
        if abs(ret) >= 0.6 * abs(gap) and (ret == 0 or gap == 0 or (ret > 0) == (gap > 0)):
            g = "GAP-AND-GONE"
        elif abs(ret) < 0.4 * abs(gap):
            g = "GAP-FILLED"
        else:
            g = "GAP-PARTIAL"
    else:
        g = "NO-GAP"

    if event_date is None:
        return g + "; NO-CATALYST-DATE"
    if bar_d is None:
        return g
    age = (bar_d - event_date).days
    if age <= 0:
        return g + "; FRESH"
    if age == 1:
        return g + "; STILL-DATED"
    return g + f"; STILL-DATED ({age}d)"


def _reason(row: dict[str, Any], filings: list[dict[str, str]], news: dict[str, str] | None) -> str:
    bits: list[str] = ["VOLUME"]
    if filings:
        bits.append("8-K")
    if news and news.get("title"):
        bits.append("NEWS")
    return " / ".join(bits)


def _one_line(row: dict[str, Any], filings: list[dict[str, str]], news: dict[str, str] | None) -> tuple[str, str]:
    """Return (fact, source_url). Prefer filing, then headline, then tape."""
    def clean(s: str) -> str:
        s = " ".join(s.split())
        s = s.replace("|", "/")
        return s[:140]

    if filings:
        f = filings[0]
        filed = f.get("filed") or ""
        if len(filed) == 8:
            filed_s = f"{filed[:4]}-{filed[4:6]}-{filed[6:]}"
        else:
            filed_s = filed
        fact = f"{f.get('form')} filed {filed_s}"
        if f.get("name"):
            fact += f" ({f['name']})"
        return clean(fact), f.get("url") or ""
    if news and news.get("title"):
        return clean(news["title"]), news.get("url") or ""
    fact = (
        f"Rel vol {row['rel_vol']:.1f}x, {row['ret_1d']*100:+.2f}% "
        f"on ${row['dollar_vol']/1e6:.0f}M dollar volume"
    )
    src = f"https://finance.yahoo.com/quote/{row['yahoo_ticker']}"
    return clean(fact), src


def render(
    day,
    universe_n: int,
    screened_n: int,
    source: str,
    top: list[dict[str, Any]],
    filings_map: dict[str, list[dict[str, str]]],
    news_map: dict[str, dict[str, str] | None],
    index_dates: list[str],
    bar_asof: str | None,
) -> str:
    now = datetime.now(PT)
    key_state = "set" if polygon_api_key() else "unset"
    lines = [
        f"# Paper desk scan — {day.isoformat()}",
        "",
        f"**Session date (PT):** {now.strftime('%A %d %b %Y %H:%M %Z')}  ",
        f"**Universe:** Wikipedia S&P 500 list, {universe_n} names (not a licensed index product).  ",
        f"**Bars:** {source} (POLYGON_API_KEY {key_state}); as-of {bar_asof or 'n/a'}; "
        f"lookback {LOOKBACK} sessions; min dollar volume ${MIN_DOLLAR_VOL:,.0f}.  ",
        f"**Screened:** {screened_n} names above the floor. Hydrated top {len(top)}.  ",
        f"**Filings:** EDGAR daily index dates {', '.join(index_dates) or 'none loaded'}.  ",
        "**Not a recommendation.** Facts only. No orders.",
        "",
        "## Ranked outliers",
        "",
        "| # | Ticker | Last | 1d % | Vol vs 20d | $ vol (M) | Reason | One-line fact | Gap flag | Source |",
        "|---|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    for i, row in enumerate(top, 1):
        filings = filings_map.get(row["ticker"], [])
        news = news_map.get(row["ticker"])
        event_dates = []
        for f in filings:
            d = _parse_filed(f.get("filed") or "")
            if d:
                event_dates.append(d)
        if news:
            d = _parse_pub(news.get("pub") or "")
            if d:
                event_dates.append(d)
        newest = max(event_dates) if event_dates else None
        reason = _reason(row, filings, news)
        fact, url = _one_line(row, filings, news)
        flag = gap_flag(row, newest)
        src = url or f"https://finance.yahoo.com/quote/{row['yahoo_ticker']}"
        # markdown link; escape pipes already handled
        src_md = f"[src]({src})"
        lines.append(
            "| {i} | {t} | {last:.2f} | {ret:+.2f} | {rv:.1f}× | {dv:.1f} | {reason} | {fact} | {flag} | {src} |".format(
                i=i,
                t=row["ticker"],
                last=row["last"],
                ret=row["ret_1d"] * 100,
                rv=row["rel_vol"],
                dv=row["dollar_vol"] / 1e6,
                reason=reason,
                fact=fact,
                flag=flag,
                src=src_md,
            )
        )
    lines += [
        "",
        "## Notes",
        "",
        f"- Reason codes: `VOLUME` (rel vol ≥ {REL_VOL_FLAG:g}× or ranked on volume), `8-K` (8-K / 10-Q / Form 4 in the EDGAR window), `NEWS` (Yahoo RSS hit).",
        f"- `GAP-AND-GONE`: overnight gap ≥ {GAP_PCT*100:.1f}% that held into the last print. `STILL-DATED`: newest headline/filing is older than the last bar date.",
        "- Headlines are best-effort public RSS. Missing news is not invented.",
        f"- Dollar-volume floor ${MIN_DOLLAR_VOL:,.0f} drops leftover/parse junk, not a mid-cap screen.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    day = _today_pt()
    ensure_blotter()
    conn = connect_db()
    try:
        try:
            constituents = load_constituents(conn)
        except ConstituentsError as exc:
            return data_fail(day, "wikipedia", str(exc))
        except Exception as exc:
            return data_fail(day, "wikipedia", str(exc))

        try:
            source = load_bars(conn, constituents)
        except BarsError as exc:
            return data_fail(day, "bars", str(exc))
        except Exception as exc:
            return data_fail(day, "bars", str(exc))

        log.info("bars source used: %s", source)
        ranked = score_universe(constituents, conn)
        if len(ranked) < 25:
            # still write if we have a handful, but empty universe is a fail
            if len(ranked) < 5:
                return data_fail(
                    day,
                    "bars",
                    f"only {len(ranked)} names passed the dollar-volume floor after {source} bars",
                )

        top = ranked[:TOP_N]
        ciks = {r.get("cik") or "" for r in top}
        ciks.discard("")
        try:
            all_filings, index_dates = fetch_daily_indexes(5)
            filtered = filter_ciks(all_filings, ciks)
            filings_map = hydrate_filings(top, filtered)
        except Exception as exc:
            log.warning("EDGAR hydrate failed (continuing): %s", exc)
            filings_map = {r["ticker"]: [] for r in top}
            index_dates = []

        news_map: dict[str, dict[str, str] | None] = {}
        news_miss = 0
        skip_rss = False
        for r in top:
            if skip_rss:
                news_map[r["ticker"]] = None
                continue
            try:
                hit = fetch_headline(r["yahoo_ticker"])
            except Exception as exc:
                log.info("headline %s: %s", r["ticker"], exc)
                hit = None
            news_map[r["ticker"]] = hit
            if hit is None:
                news_miss += 1
                if news_miss >= 3:
                    skip_rss = True
                    log.warning("Yahoo RSS empty/429; skipping remaining headlines")
            else:
                news_miss = 0

        md = render(
            day,
            universe_n=len(constituents),
            screened_n=len(ranked),
            source=source,
            top=top,
            filings_map=filings_map,
            news_map=news_map,
            index_dates=index_dates,
            bar_asof=last_session(conn),
        )
        write_notes(day, md)
        log.info("scan complete source=%s top=%s", source, ",".join(r["ticker"] for r in top[:10]))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
