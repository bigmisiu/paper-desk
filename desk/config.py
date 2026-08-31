"""Paths, knobs, and a tiny .env loader. No secrets in defaults."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
NOTES_DIR = ROOT / "notes"
BLOTTER_PATH = ROOT / "blotter.csv"
DB_PATH = DATA_DIR / "scan.sqlite"
ENV_PATH = ROOT / ".env"

TOP_N = 25
LOOKBACK = 20
MIN_DOLLAR_VOL = 50_000_000  # skip illiquid leftovers / parse junk
CONSTITUENTS_MAX_AGE_DAYS = 7
HTTP_TIMEOUT = 30
HTTP_RETRIES = 5
SEC_MIN_INTERVAL = 0.12  # SEC fair-access: well under 10 req/s

DEFAULT_SEC_UA = "paper-desk/0.1 (research; contact: operator@localhost)"
WIKI_UA = (
    "paper-desk/0.1 (research scanner; contact: operator@localhost) "
    "Python-requests"
)
YAHOO_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

BLOTTER_HEADER = (
    "date,ticker,side,shares,entry_ref,slip_bps,size_usd,nav_pct,"
    "thesis,invalidation,time_stop,status,daily_mark,exit_reason,book"
)


def load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


load_dotenv()


def polygon_api_key() -> str:
    return os.environ.get("POLYGON_API_KEY", "").strip()


def sec_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    return ua or DEFAULT_SEC_UA


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS constituents (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            cik TEXT,
            yahoo_ticker TEXT,
            polygon_ticker TEXT,
            fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS bars (
            ticker TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            source TEXT,
            PRIMARY KEY (ticker, date)
        );
        """
    )
    conn.commit()
    return conn


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
