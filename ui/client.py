"""
A small client for the Research Desk API (SPEC §5), used by the Streamlit app (ui/app.py).

It only speaks HTTP: no AWS SDK, no core/ imports, so the UI deploys with streamlit + requests.
The access code goes in the `x-access-code` header on every call. Errors come back as DeskError
with the API's own safe message (the API never returns stack traces).
"""

import time

import requests

POLL_SECONDS = 2.0
MAX_WAIT_SECONDS = 150      # Deep's budget is 75 s; the worker's timeout is 120 s
TIMEOUT_SECONDS = 15
DONE_STATES = ("done", "error")


class DeskError(Exception):
    """A user-safe message: wrong code, a bad request, the API down, or a job that never finished."""


class DeskClient:
    def __init__(self, base_url: str, access_code: str, session=None, timeout: float = TIMEOUT_SECONDS):
        if not base_url or not access_code:
            raise DeskError("The API URL and access code aren't configured (Streamlit secrets).")
        self.base_url = base_url.rstrip("/")
        self.access_code = access_code
        self.session = session or requests.Session()
        self.timeout = timeout

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            r = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout,
                                     headers={"x-access-code": self.access_code}, **kwargs)
        except requests.RequestException as e:
            raise DeskError(f"The API can't be reached ({type(e).__name__}). Try again in a moment.") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 401:
            raise DeskError("The API rejected the access code (401). Check the app's secrets.")
        if r.status_code == 429:
            raise DeskError("Too many requests right now (the API is throttled). Try again in a few seconds.")
        if r.status_code >= 400:
            raise DeskError(f"{body.get('error') or 'Request failed'} (HTTP {r.status_code})")
        return body

    # ── routes ──
    def health(self) -> bool:
        return self._call("GET", "/health").get("ok") is True

    def watchlist(self) -> list[dict]:
        return self._call("GET", "/watchlist").get("watchlist", [])

    def quotes(self) -> dict:
        return self._call("GET", "/quotes").get("quotes", {})

    def feed(self, limit: int = 20) -> list[dict]:
        return self._call("GET", "/feed", params={"limit": limit}).get("events", [])

    def ask(self, question: str, mode: str) -> str:
        """Starts an answer; returns its job_id (POST /ask -> 202)."""
        return self._call("POST", "/ask", json={"question": question, "mode": mode})["job_id"]

    def job(self, job_id: str) -> dict:
        return self._call("GET", f"/ask/{job_id}")

    def wait(self, job_id: str, on_poll=None, poll_seconds: float = POLL_SECONDS,
             max_seconds: float = MAX_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic) -> dict:
        """Polls until the job is done or failed. `on_poll(seconds_waited)` lets the UI show progress."""
        started = clock()
        while True:
            body = self.job(job_id)
            if body.get("status") in DONE_STATES:
                return body
            waited = clock() - started
            if waited >= max_seconds:
                raise DeskError(f"No answer after {max_seconds:.0f} s. Try again, or use Quick mode.")
            if on_poll:
                on_poll(waited)
            sleep(poll_seconds)
