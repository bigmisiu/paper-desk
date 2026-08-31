#!/usr/bin/env bash
# One-shot. Run as your user. No sudo. No /opt.
set -euo pipefail

REPO_URL="https://github.com/bigmisiu/paper-desk.git"
DEST="${PAPER_DESK_HOME:-$HOME/paper-desk}"

if [[ ! -f "$DEST/requirements.txt" ]]; then
  mkdir -p "$(dirname "$DEST")"
  if [[ -d "$DEST/.git" ]]; then
    git -C "$DEST" pull --ff-only
  else
    git clone "$REPO_URL" "$DEST"
  fi
fi

cd "$DEST"

if [[ -d .venv && ! -w .venv ]]; then
  echo "stale root venv at $DEST/.venv — run: sudo rm -rf $DEST/.venv" >&2
  exit 1
fi

python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
[[ -f .env ]] || cp .env.example .env

CRON_FILE="$(mktemp)"
{
  echo "SHELL=/bin/bash"
  echo "CRON_TZ=America/Los_Angeles"
  echo "30 6 * * 1-5 cd $DEST && .venv/bin/python -m desk >> $DEST/data/cron.log 2>&1"
} > "$CRON_FILE"
# replace any prior paper-desk cron line, keep the rest
(crontab -l 2>/dev/null | grep -v "paper-desk" | grep -v "python -m desk" || true; cat "$CRON_FILE") | crontab -
rm -f "$CRON_FILE"
mkdir -p "$DEST/data"

echo "installed at $DEST"
echo "edit $DEST/.env then: $DEST/.venv/bin/python -m desk"
