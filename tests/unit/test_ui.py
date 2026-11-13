"""The Streamlit UI (ui/): the API client with a fake HTTP session, and the app headless with
Streamlit's AppTest (secrets, the viewer gate, an answer). No network."""

import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
sys.path.insert(0, str(UI))   # Streamlit runs ui/app.py with ui/ on the path, so `import client`

import client as ui_client  # noqa: E402

URL, CODE = "https://api.example.test", "letmein-test-code"


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_every_call_sends_the_access_code_and_reads_the_body():
    s = FakeSession(FakeResponse(200, {"watchlist": [{"ticker": "NVDA"}]}),
                    FakeResponse(202, {"job_id": "a" * 32, "status": "pending"}),
                    FakeResponse(200, {"events": []}))
    c = ui_client.DeskClient(URL + "/", CODE, session=s)
    assert c.watchlist() == [{"ticker": "NVDA"}]
    assert c.ask("What is AMD's stock price?", "deep") == "a" * 32
    assert c.feed(limit=5) == []
    assert [(m, u) for m, u, _ in s.calls] == [("GET", URL + "/watchlist"), ("POST", URL + "/ask"),
                                               ("GET", URL + "/feed")]
    assert all(kw["headers"] == {"x-access-code": CODE} and kw["timeout"] for _, _, kw in s.calls)
    assert s.calls[1][2]["json"] == {"question": "What is AMD's stock price?", "mode": "deep"}
    assert s.calls[2][2]["params"] == {"limit": 5}


@pytest.mark.parametrize("response, message", [
    (FakeResponse(401, {"error": "Missing or wrong x-access-code"}), "rejected the access code"),
    (FakeResponse(429, {"message": "Too Many Requests"}), "throttled"),
    (FakeResponse(400, {"error": "'mode' must be one of quick, deep"}), "'mode' must be one of quick, deep (HTTP 400)"),
    (FakeResponse(502, ValueError("not json")), "Request failed (HTTP 502)"),
    (requests.ConnectionError("down"), "can't be reached (ConnectionError)"),
])
def test_errors_become_readable_messages(response, message):
    c = ui_client.DeskClient(URL, CODE, session=FakeSession(response))
    with pytest.raises(ui_client.DeskError, match=message.replace("(", r"\(").replace(")", r"\)")):
        c.quotes()


def test_missing_configuration_is_a_clear_error():
    with pytest.raises(ui_client.DeskError, match="aren't configured"):
        ui_client.DeskClient("", CODE)


def test_wait_polls_until_done_and_reports_progress():
    pending = FakeResponse(200, {"status": "pending"})
    s = FakeSession(pending, pending, FakeResponse(200, {"status": "done", "answer": "ok"}))
    c = ui_client.DeskClient(URL, CODE, session=s)
    clock, slept, seen = iter([0, 2, 4, 6]), [], []
    out = c.wait("b" * 32, on_poll=seen.append, sleep=slept.append, clock=lambda: next(clock))
    assert out["answer"] == "ok" and slept == [2.0, 2.0] and seen == [2, 4]
    assert all(u == f"{URL}/ask/{'b' * 32}" for _, u, _ in s.calls)


def test_wait_returns_an_error_job_and_gives_up_after_the_limit():
    c = ui_client.DeskClient(URL, CODE, session=FakeSession(FakeResponse(200, {"status": "error", "error": "x"})))
    assert c.wait("c" * 32, sleep=lambda s: None)["status"] == "error"
    pending = FakeResponse(200, {"status": "pending"})
    c = ui_client.DeskClient(URL, CODE, session=FakeSession(pending, pending))
    clock = iter([0, 100, 200])
    with pytest.raises(ui_client.DeskError, match="No answer after 150 s"):
        c.wait("c" * 32, sleep=lambda s: None, clock=lambda: next(clock))


# ── the app, headless ──

@pytest.fixture
def fake_api(monkeypatch):
    calls = []
    monkeypatch.setattr(ui_client.DeskClient, "watchlist", lambda self: [{"ticker": "NVDA", "pct_move": 3.0}])
    monkeypatch.setattr(ui_client.DeskClient, "quotes", lambda self: {"NVDA": {
        "price": 176.4, "change_pct": -3.2, "trading_date": "2026-11-13", "source": "yfinance"}})
    monkeypatch.setattr(ui_client.DeskClient, "feed", lambda self, limit=20: [
        {"title": "NVDA -3.2% on Nov 13", "body": "NVIDIA is at $176.40", "created_at": "2026-11-13T18:00:21"}])
    monkeypatch.setattr(ui_client.DeskClient, "ask", lambda self, q, m: calls.append((q, m)) or "d" * 32)
    monkeypatch.setattr(ui_client.DeskClient, "wait", lambda self, job_id, on_poll=None: {
        "status": "done", "mode": "deep", "answer": "NVIDIA trades at $176.40 [E1].", "as_of": "2026-11-13T18:01:00",
        "timings": {"total": 5.7, "llm_calls": 3},
        "sources": [{"id": "E1", "label": "[E1] yfinance quote: NVDA", "url": "https://x.test", "cited": True}],
        "trace": {"fast_path": True, "plan": {"tasks": [{"agent": "market"}]}, "rounds": [[
            {"agent": "market", "seconds": 3.2, "findings": [{}], "gaps": [], "steps": [{"action": "tool", "tool": "get_quote"}]}]],
            "checker": {"numbers_checked": 1, "supported": 1, "derived": [], "unsupported": []}}})
    return calls


def _app(secrets: dict):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(UI / "app.py"), default_timeout=30)
    for k, v in secrets.items():
        at.secrets[k] = v
    return at.run()


def test_app_without_secrets_says_what_is_missing():
    at = _app({})
    assert "Missing secrets" in at.error[0].value


def test_app_gate_rejects_a_wrong_code_and_opens_with_the_right_one(fake_api):
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    assert at.text_input[0].label == "Access code" and not at.button
    at.text_input[0].input("nope").run()
    assert at.error[0].value == "Wrong access code."
    at.text_input[0].input(CODE).run()
    assert not at.error and [m.label for m in at.sidebar.metric] == ["NVDA"]
    assert any("NVDA -3.2% on Nov 13" in m.value for m in at.markdown)


def test_app_asks_in_the_chosen_mode_and_shows_the_answer(fake_api):
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    at.text_input[0].input(CODE).run()
    at.text_input[0].input("What is NVIDIA's stock price right now?")
    at.radio[0].set_value("Deep")
    at.button[0].click().run()   # the form's Ask button
    assert fake_api == [("What is NVIDIA's stock price right now?", "deep")]
    text = " ".join(m.value for m in at.markdown)
    assert r"NVIDIA trades at \$176.40 [E1]." in text           # $ escaped, not read as maths
    assert "fast path" in text and "1/1 numbers matched" in text
