"""S&P 500-class universe from Wikipedia. Cached, refresh at most weekly.

Not a licensed index. We parse the public HTML table and store ticker+CIK.
"""

from __future__ import annotations

import html as htmlmod
import logging
import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from desk.config import CONSTITUENTS_MAX_AGE_DAYS, WIKI_UA, meta_get, meta_set
from desk import http

log = logging.getLogger("desk.constituents")

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
MIN_UNIVERSE = 400


class _TableParser(HTMLParser):
    """Grab rows from table#constituents; ignore nested tables."""

    def __init__(self, table_id: str = "constituents") -> None:
        super().__init__(convert_charrefs=True)
        self.table_id = table_id
        self.in_target = False
        self.depth = 0
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] | None = None
        self.saw_id = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        if tag == "table" and not self.in_target:
            if ad.get("id") == self.table_id:
                self.in_target = True
                self.depth = 1
                self.saw_id = True
            return
        if not self.in_target:
            return
        if tag == "table":
            self.depth += 1
            return
        if self.depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if not self.in_target:
            return
        if tag in ("td", "th") and self._cell is not None:
            text = htmlmod.unescape(re.sub(r"\s+", " ", "".join(self._cell))).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._row = []
        elif tag == "table":
            self.depth -= 1
            if self.depth <= 0:
                self.in_target = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


class _FirstWikitable(HTMLParser):
    """Fallback: first wikitable whose header looks like the constituents list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._in = False
        self._is_wiki = False
        self._rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        if tag == "table":
            if not self._in:
                cls = ad.get("class", "")
                self._is_wiki = "wikitable" in cls
                if self._is_wiki:
                    self._in = True
                    self._depth = 1
                    self._rows = []
                return
            if self._in:
                self._depth += 1
            return
        if not self._in or self._depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if not self._in:
            return
        if tag in ("td", "th") and self._cell is not None:
            text = htmlmod.unescape(re.sub(r"\s+", " ", "".join(self._cell))).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr":
            if self._row:
                self._rows.append(self._row)
            self._row = []
        elif tag == "table":
            self._depth -= 1
            if self._depth <= 0:
                if self._rows:
                    self.tables.append(self._rows)
                self._in = False
                self._rows = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def yahoo_ticker(wiki_ticker: str) -> str:
    # Wikipedia BRK.B / BF.B → Yahoo BRK-B / BF-B
    return wiki_ticker.replace(".", "-")


def polygon_ticker(wiki_ticker: str) -> str:
    return wiki_ticker.replace("-", ".")


def _col(headers: list[str], *needles: str) -> int | None:
    lower = [h.lower().strip() for h in headers]
    for needle in needles:
        for i, h in enumerate(lower):
            if needle == h or needle in h:
                return i
    return None


def _rows_to_constituents(rows: list[list[str]]) -> list[dict[str, str]]:
    if not rows:
        return []
    headers = rows[0]
    i_sym = _col(headers, "symbol", "ticker")
    i_cik = _col(headers, "cik")
    i_name = _col(headers, "security", "company", "name")
    if i_sym is None:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if i_sym >= len(row):
            continue
        raw = row[i_sym].strip().upper().replace(" ", "")
        ticker = re.sub(r"[^A-Z0-9.\-]", "", raw)
        if not ticker or ticker in seen or ticker == "SYMBOL":
            continue
        cik = ""
        if i_cik is not None and i_cik < len(row):
            digits = re.sub(r"\D", "", row[i_cik])
            if digits:
                cik = digits.zfill(10)
        name = ""
        if i_name is not None and i_name < len(row):
            name = row[i_name].strip()
        seen.add(ticker)
        out.append(
            {
                "ticker": ticker,
                "name": name,
                "cik": cik,
                "yahoo_ticker": yahoo_ticker(ticker),
                "polygon_ticker": polygon_ticker(ticker),
            }
        )
    return out


def parse_wikipedia_html(html: str) -> list[dict[str, str]]:
    p = _TableParser()
    p.feed(html)
    names = _rows_to_constituents(p.rows)
    if len(names) >= MIN_UNIVERSE:
        return names
    fb = _FirstWikitable()
    fb.feed(html)
    best: list[dict[str, str]] = names
    for table in fb.tables:
        cand = _rows_to_constituents(table)
        if len(cand) > len(best):
            best = cand
    return best


class ConstituentsError(RuntimeError):
    pass


def _load_cache(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT ticker, name, cik, yahoo_ticker, polygon_ticker FROM constituents ORDER BY ticker"
    ).fetchall()
    return [dict(r) for r in rows]


def _save_cache(conn: sqlite3.Connection, rows: list[dict[str, str]], fetched_at: str) -> None:
    conn.execute("DELETE FROM constituents")
    conn.executemany(
        """
        INSERT INTO constituents(ticker, name, cik, yahoo_ticker, polygon_ticker, fetched_at)
        VALUES (:ticker, :name, :cik, :yahoo_ticker, :polygon_ticker, :fetched_at)
        """,
        [{**r, "fetched_at": fetched_at} for r in rows],
    )
    meta_set(conn, "constituents_fetched_at", fetched_at)
    conn.commit()


def _cache_age_days(conn: sqlite3.Connection) -> float | None:
    raw = meta_get(conn, "constituents_fetched_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    except ValueError:
        return None


def fetch_wikipedia() -> list[dict[str, str]]:
    log.info("fetching Wikipedia constituents %s", WIKI_URL)
    resp = http.get(WIKI_URL, headers={"User-Agent": WIKI_UA, "Accept": "text/html"})
    if resp.status_code != 200:
        raise ConstituentsError(f"Wikipedia HTTP {resp.status_code}")
    rows = parse_wikipedia_html(resp.text)
    if len(rows) < MIN_UNIVERSE:
        raise ConstituentsError(
            f"Wikipedia table parsed {len(rows)} rows (need >= {MIN_UNIVERSE}); table shape may have changed"
        )
    log.info("Wikipedia parsed %d constituents", len(rows))
    return rows


def load_constituents(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Return cached rows; refresh from Wikipedia if older than a week."""
    age = _cache_age_days(conn)
    cached = _load_cache(conn)
    if cached and age is not None and age < CONSTITUENTS_MAX_AGE_DAYS:
        log.info("constituents cache hit (%d names, %.1f days old)", len(cached), age)
        return cached
    try:
        fresh = fetch_wikipedia()
        now = datetime.now(timezone.utc).isoformat()
        _save_cache(conn, fresh, now)
        return fresh
    except Exception as exc:
        if cached:
            log.warning("Wikipedia refresh failed (%s); using cache n=%d", exc, len(cached))
            return cached
        raise ConstituentsError(f"Wikipedia failed and no cache: {exc}") from exc
