"""POST the morning note to Gekko. Secrets stay in .env on the VPS."""

from __future__ import annotations

import json
import logging
import os
from datetime import date

import requests

from desk.config import HTTP_TIMEOUT

log = logging.getLogger("desk.notify")


def webhook_url() -> str:
    return os.environ.get("DESK_WEBHOOK_URL", "").strip()


def webhook_secret() -> str:
    return os.environ.get("DESK_WEBHOOK_SECRET", "").strip()


def post_scan(day: date, markdown: str) -> None:
    url = webhook_url()
    if not url:
        log.info("DESK_WEBHOOK_URL unset; scan stays local")
        return
    secret = webhook_secret()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "paper-desk/0.1",
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
        headers["X-Webhook-Secret"] = secret
        headers["X-Api-Key"] = secret
    payload = {
        "source": "paper-desk-vps",
        "date": day.isoformat(),
        "markdown": markdown,
        "data_fail": markdown.lstrip().startswith("# DATA_FAIL"),
    }
    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=HTTP_TIMEOUT)
        if resp.status_code >= 300:
            log.warning("webhook %s %s %s", url, resp.status_code, resp.text[:300])
            return
        log.info("webhook ok %s", resp.status_code)
    except requests.RequestException as exc:
        log.warning("webhook failed: %s", exc)
