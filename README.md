# paper-desk drop-in scanner

Morning facts sheet for liquid US large-caps (S&P 500-class names from the public Wikipedia list). Two-stage scan, one weekday cron job, markdown artifact. **Facts only — no buy/sell recommendations, no broker, no orders.**

This is not a licensed index product. Constituents are scraped from Wikipedia's public S&P 500 table and cached locally.

## Quickstart (VPS)

From the directory that contains this README:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
(crontab -l 2>/dev/null; cat crontab.example) | crontab -
```

Edit `.env`: Polygon key (optional but recommended), a real email in `SEC_USER_AGENT`, and the Gekko webhook URL + sender key from the routine **Paper desk scan inbound**. Never commit `.env`. Edit the path in `crontab.example` (default `/opt/paper-desk-drop`) before installing cron. Cron line is `30 6 * * 1-5` with `CRON_TZ=America/Los_Angeles`. After each run the scanner POSTs `notes/YYYY-MM-DD.md` to Gekko so you do not paste files.

Manual run (same as cron):

```bash
.venv/bin/python -m desk
```

Python 3.11 or newer. Writes:

- `notes/YYYY-MM-DD.md` — ranked table (Pacific date)
- `data/scan.sqlite` — bars + constituents cache
- `blotter.csv` — header created if missing, never overwritten

## What it does

1. **Cheap screen.** Wikipedia constituents (refresh at most weekly) + daily bars. Drop names below the dollar-volume floor. Rank by volume vs ~20-session average, then |1d %|.
2. **Hydrate top 25.** Best-effort Yahoo RSS headlines + SEC EDGAR daily index (8-K / 10-Q / Form 4) filtered to those CIKs, with company-submissions as a backfill.
3. **Markdown artifact.** Ticker, last, 1d %, vol vs 20d, reason codes (`VOLUME` / `8-K` / `NEWS`), one-line fact, source URL. Gap flag: `GAP-AND-GONE` vs `STILL-DATED` (and fill/partial when the gap came back).

If Wikipedia cannot be fetched **and** there is no cache, or bars cannot be fetched (Yahoo/Polygon) for a usable universe, the job writes `notes/YYYY-MM-DD.md` containing `DATA_FAIL` and exits 1.

## Bars

- `POLYGON_API_KEY` set: Polygon grouped daily aggregates. On 401/403/empty, falls back to Yahoo and logs it.
- Key unset: Yahoo Chart v8. Never crashes because Polygon is missing.

## Limitations

- **Yahoo** Chart v8 rate-limits datacenter IPs (HTTP 429) and sometimes 401s without a crumb cookie. The client retries 429s, warms `finance.yahoo.com` cookies, and reuses `data/scan.sqlite` if a prior run stored bars. A full Yahoo outage with an empty cache is `DATA_FAIL`. A Polygon key avoids this path.
- **Wikipedia** table shape (column names, `id="constituents"`) can change. Parser matches Symbol/CIK headers; if the table cannot be parsed and cache is stale/missing → `DATA_FAIL`.
- **Polygon** grouped-daily (`/v2/aggs/grouped/...`) is a stocks-plan endpoint. Free/delayed tiers may 403; previous-close-only plans are not enough. Fallback to Yahoo is automatic and logged.
- **EDGAR** daily index for *today* is often empty at 6:30am PT; the job reads the last few calendar days. `www.sec.gov/Archives` is Akamai-walled from some cloud IPs (403); company submissions on `data.sec.gov` are the backfill. Put a real email in `SEC_USER_AGENT`.
- Headlines are public RSS/Yahoo, best-effort, not a news firehose. No options, crypto, leverage, or broker keys.

## Layout

```
desk/           # python -m desk
notes/          # dated markdown
data/           # scan.sqlite (gitignored)
blotter.csv     # paper blotter header
crontab.example
```
