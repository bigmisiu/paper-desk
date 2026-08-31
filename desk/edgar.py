"""SEC EDGAR daily index + company submissions. 8-K / 10-Q / Form 4."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from desk.config import SEC_MIN_INTERVAL, sec_user_agent
from desk import http

log = logging.getLogger("desk.edgar")

ET = ZoneInfo("America/New_York")
DAILY_INDEX = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/master.{ymd}.idx"
)
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES = "https://www.sec.gov/Archives/"

WATCH_FORMS = {
    "8-K",
    "8-K/A",
    "10-Q",
    "10-Q/A",
    "10-K",
    "10-K/A",
    "4",
    "4/A",
}


def _headers() -> dict[str, str]:
    return {
        "User-Agent": sec_user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "text/plain,application/json,*/*",
    }


def _data_headers() -> dict[str, str]:
    return {
        "User-Agent": sec_user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }


def _quarter(d: date) -> int:
    return (d.month - 1) // 3 + 1


def _index_dates(n: int = 5) -> list[date]:
    cursor = datetime.now(ET).date()
    out: list[date] = []
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return out


def parse_master_idx(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("CIK|") or line.startswith("cik|"):
            start = i + 1
            break
        if set(line.strip()) <= {"-", " " } and len(line.strip()) > 10:
            start = i + 1
            break
    rows: list[dict[str, str]] = []
    for line in lines[start:]:
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        cik, name, form, filed, path = (p.strip() for p in parts[:5])
        form_u = form.upper()
        if form_u not in WATCH_FORMS:
            continue
        digits = "".join(ch for ch in cik if ch.isdigit())
        if not digits:
            continue
        rows.append(
            {
                "cik": digits.zfill(10),
                "name": name,
                "form": form_u,
                "filed": filed.replace("-", ""),
                "path": path.lstrip("/"),
                "url": ARCHIVES + path.lstrip("/"),
            }
        )
    return rows


def fetch_daily_indexes(lookback_days: int = 5) -> tuple[list[dict[str, str]], list[str]]:
    """Return (filings, index dates that actually loaded)."""
    filings: list[dict[str, str]] = []
    loaded: list[str] = []
    for d in _index_dates(lookback_days):
        ymd = d.strftime("%Y%m%d")
        url = DAILY_INDEX.format(year=d.year, q=_quarter(d), ymd=ymd)
        try:
            resp = http.get(url, headers=_headers(), timeout=45, retries=4)
        except Exception as exc:
            log.warning("EDGAR index %s error: %s", ymd, exc)
            time.sleep(SEC_MIN_INTERVAL)
            continue
        time.sleep(SEC_MIN_INTERVAL)
        if resp.status_code == 404:
            log.info("EDGAR index %s not published yet", ymd)
            continue
        if resp.status_code == 403:
            log.warning("EDGAR index %s 403 (archives bot-wall); submissions backfill will be used", ymd)
            continue
        if resp.status_code != 200:
            log.warning("EDGAR index %s HTTP %s", ymd, resp.status_code)
            continue
        if "<html" in resp.text[:200].lower():
            log.warning("EDGAR index %s returned HTML, not master.idx", ymd)
            continue
        batch = parse_master_idx(resp.text)
        log.info("EDGAR index %s: %d watched forms", ymd, len(batch))
        filings.extend(batch)
        loaded.append(d.isoformat())
    return filings, loaded


def filter_ciks(filings: list[dict[str, str]], ciks: set[str]) -> list[dict[str, str]]:
    want = {c.zfill(10) for c in ciks if c}
    return [f for f in filings if f["cik"] in want]


def accession_url(cik: str, accession: str, primary: str) -> str:
    cik_n = str(int(cik))
    acc_nodash = accession.replace("-", "")
    return f"{ARCHIVES}edgar/data/{cik_n}/{acc_nodash}/{primary}"


def fetch_submissions(cik: str) -> list[dict[str, str]]:
    url = SUBMISSIONS.format(cik=cik.zfill(10))
    try:
        resp = http.get(url, headers=_data_headers(), timeout=30, retries=3)
    except Exception as exc:
        log.warning("submissions %s error: %s", cik, exc)
        return []
    if resp.status_code != 200:
        log.warning("submissions %s HTTP %s", cik, resp.status_code)
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    out: list[dict[str, str]] = []
    for i, form in enumerate(forms[:40]):
        fu = str(form).upper()
        if fu not in WATCH_FORMS:
            continue
        filed = (dates[i] if i < len(dates) else "").replace("-", "")
        acc = accs[i] if i < len(accs) else ""
        doc = docs[i] if i < len(docs) else ""
        out.append(
            {
                "cik": cik.zfill(10),
                "name": data.get("name") or "",
                "form": fu,
                "filed": filed,
                "path": "",
                "url": accession_url(cik, acc, doc) if acc and doc else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik.zfill(10)}&type={fu}&count=10",
            }
        )
    return out


def hydrate_filings(
    top: list[dict[str, Any]],
    index_filings: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    """Map canonical ticker -> recent filings (daily index, submissions backfill)."""
    by_cik: dict[str, list[dict[str, str]]] = {}
    for f in index_filings:
        by_cik.setdefault(f["cik"], []).append(f)

    out: dict[str, list[dict[str, str]]] = {}
    for row in top:
        cik = (row.get("cik") or "").zfill(10) if row.get("cik") else ""
        ticker = row["ticker"]
        found = list(by_cik.get(cik, [])) if cik else []
        if not found and cik:
            time.sleep(SEC_MIN_INTERVAL)
            found = fetch_submissions(cik)
            # keep only last ~5 calendar days
            cutoff = (datetime.now(ET).date() - timedelta(days=7)).strftime("%Y%m%d")
            found = [f for f in found if f.get("filed", "") >= cutoff]
        # de-dupe by form+filed+url
        seen: set[tuple[str, str, str]] = set()
        uniq: list[dict[str, str]] = []
        for f in found:
            key = (f.get("form", ""), f.get("filed", ""), f.get("url", ""))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(f)
        uniq.sort(key=lambda x: x.get("filed", ""), reverse=True)
        out[ticker] = uniq
    return out
