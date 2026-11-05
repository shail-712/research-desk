"""SEC client: contact User-Agent, retries on 429/5xx only, safe errors. requests mocked."""

from types import SimpleNamespace

import pytest

from core import sec


@pytest.fixture
def ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")
    monkeypatch.setattr(sec.time, "sleep", lambda s: None)


def _script(monkeypatch, statuses):
    calls = []
    replies = iter(statuses)

    def get(url, headers=None, timeout=None):
        calls.append({"headers": headers, "timeout": timeout})
        status = next(replies)
        if isinstance(status, Exception):
            raise status
        return SimpleNamespace(status_code=status)

    monkeypatch.setattr(sec.requests, "get", get)
    return calls


def test_sends_contact_user_agent_with_timeout(ua, monkeypatch):
    calls = _script(monkeypatch, [200])
    assert sec.get("https://data.sec.gov/x").status_code == 200
    assert calls[0]["headers"]["User-Agent"] == "Test Person test@example.com" and calls[0]["timeout"]


def test_retries_rate_limits_and_connection_errors(ua, monkeypatch):
    calls = _script(monkeypatch, [429, sec.requests.ConnectionError("x"), 200])
    assert sec.get("https://data.sec.gov/x").status_code == 200
    assert len(calls) == 3


def test_does_not_retry_not_found_and_raises_safe_error(ua, monkeypatch):
    calls = _script(monkeypatch, [404])
    with pytest.raises(sec.SecError, match="HTTP 404"):
        sec.get("https://data.sec.gov/x")
    assert len(calls) == 1


def test_gives_up_after_retries(ua, monkeypatch):
    _script(monkeypatch, [503] * sec.RETRIES)
    with pytest.raises(sec.SecError, match="HTTP 503"):
        sec.get("https://data.sec.gov/x")
