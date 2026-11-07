"""
Small SEC EDGAR HTTP client.

SEC rules (https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data):
- every request must send a User-Agent with a contact email (SEC_USER_AGENT),
  otherwise SEC answers 403;
- at most 10 requests per second. We stay well under with a global throttle.

Ported from v1 core/sec.py (73b6fda).
"""

import logging
import threading
import time

import requests

from core.config import env

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 0.15  # ≈ 6.7 requests/s, under SEC's 10/s limit
TIMEOUT_SECONDS = 20
RETRIES = 3

_lock = threading.Lock()
_last_request = 0.0


class SecError(RuntimeError):
    """SEC EDGAR could not be reached or refused the request."""


def user_agent() -> str:
    return env("SEC_USER_AGENT")


def is_configured() -> bool:
    return "@" in user_agent()


def get(url: str) -> requests.Response:
    """Throttled GET with retries on 429/5xx. Raises SecError."""
    global _last_request
    if not is_configured():
        raise SecError("SEC_USER_AGENT is not set (SEC requires 'Name email@example.com')")

    for attempt in range(1, RETRIES + 1):
        with _lock:
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        try:
            response = requests.get(
                url,
                headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            error = f"{type(e).__name__}"
        else:
            if response.status_code == 200:
                return response
            error = f"HTTP {response.status_code}"
            if response.status_code not in (429, 500, 502, 503, 504):
                break
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    logger.warning("SEC request failed (%s): %s", error, url)
    raise SecError(f"SEC request failed ({error})")
