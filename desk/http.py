"""requests wrapper: timeouts, 429/5xx retries, no infinite hangs."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

from desk.config import HTTP_RETRIES, HTTP_TIMEOUT

log = logging.getLogger("desk.http")

_SESSION = requests.Session()


def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return min(max(float(raw), 1.0), 120.0)
        except ValueError:
            pass
    return min(2 ** attempt + random.random(), 60.0)


def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
    session: requests.Session | None = None,
) -> requests.Response:
    sess = session or _SESSION
    last_exc: Exception | None = None
    last_resp: requests.Response | None = None
    for attempt in range(retries):
        try:
            resp = sess.get(url, headers=headers, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            sleep = min(2 ** attempt, 30)
            log.warning("GET %s error %s; retry in %.1fs", url, exc, sleep)
            time.sleep(sleep)
            continue
        last_resp = resp
        if resp.status_code == 429:
            wait = _retry_after_seconds(resp, attempt)
            log.warning("GET %s 429; retry in %.1fs", url, wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = min(2 ** attempt, 30)
            log.warning("GET %s %s; retry in %.1fs", url, resp.status_code, wait)
            time.sleep(wait)
            continue
        return resp
    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise requests.HTTPError(f"exhausted retries for {url}")


def get_ok(
    url: str,
    **kwargs: Any,
) -> requests.Response:
    resp = get(url, **kwargs)
    resp.raise_for_status()
    return resp
