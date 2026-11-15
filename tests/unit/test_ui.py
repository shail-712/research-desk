"""The Streamlit UI (ui/): the API client with a fake HTTP session, the display helpers (ui/view.py), and
the app headless with Streamlit's AppTest (secrets, the viewer gate, the tabs, an answer, state kept
across reruns). No network."""

import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
sys.path.insert(0, str(UI))   # Streamlit runs ui/app.py with ui/ on the path, so `import client`

import client as ui_client  # noqa: E402
import view  # noqa: E402

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
        {"kind": "price_move", "title": "INTC -7.5% on Nov 13 (replay)", "dedupe_key": "replay:price_move:INTC:2026-11-13",
         "body": "Demo replay: this move did not happen.", "created_at": "2026-11-13T19:10:00"},
        {"kind": "price_move", "title": "NVDA -3.2% on Nov 13", "dedupe_key": "price_move:NVDA:2026-11-13",
         "body": "NVIDIA is at $176.40", "created_at": "2026-11-13T18:00:21"}])
    monkeypatch.setattr(ui_client.DeskClient, "ask", lambda self, q, m: calls.append((q, m)) or "d" * 32)
    monkeypatch.setattr(ui_client.DeskClient, "wait", lambda self, job_id, on_poll=None: {
        "status": "done", "mode": "deep", "answer": "NVIDIA trades at $176.40 [E1].", "as_of": "2026-11-13T18:01:00",
        "timings": {"total": 5.7, "llm_calls": 3},
        "sources": [{"id": "E1", "label": "[E1] yfinance quote: NVDA", "url": "https://x.test", "cited": True}],
        "trace": {"fast_path": True, "plan": {"tasks": [{"agent": "market"}]}, "rounds": [[
            {"agent": "market", "seconds": 3.2, "findings": [{}], "gaps": [], "steps": [{"action": "tool", "tool": "get_quote"}]}]],
            "checker": {"numbers_checked": 1, "supported": 1, "derived": [], "unsupported": []}}})
    return calls


@pytest.fixture(autouse=True)
def _no_local_secrets(tmp_path, monkeypatch):
    """Hide any real secrets.toml: Streamlit reads ./.streamlit and ~/.streamlit, so point both at an empty
    folder and start from a fresh, unparsed st.secrets."""
    import streamlit as st
    from streamlit.runtime.secrets import Secrets

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(st, "secrets", Secrets())
    st.cache_data.clear()   # the app caches quotes and the feed; each test starts from its own fake API


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
    assert not at.error and [m.label for m in at.metric] == ["NVDA"] and at.metric[0].value == "$176.40"
    assert [t.label for t in at.tabs] == ["Ask", "Watchlist", "Alerts", "How it works"]
    assert any("NVDA -3.2% on Nov 13" in m.value for m in at.markdown)


def test_app_asks_in_the_chosen_mode_and_shows_the_answer(fake_api):
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    at.text_input[0].input(CODE).run()
    at.text_input[0].input("What is NVIDIA's stock price right now?")
    at.radio[0].set_value("Deep")
    ask_button(at).click().run()
    assert fake_api == [("What is NVIDIA's stock price right now?", "deep")]
    text = " ".join(m.value for m in at.markdown)
    assert r"NVIDIA trades at \$176.40 [E1]." in text           # $ escaped, not read as maths
    assert "fast path" in text and "1/1 numbers matched" in text


def ask_button(at):
    return next(b for b in at.button if b.label == "Ask")


def _markdown(at) -> str:
    return " ".join(m.value for m in at.markdown)


def _captions(at) -> str:
    return " ".join(c.value for c in at.caption)


def test_alerts_are_cards_and_only_a_replay_is_marked(fake_api):
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    at.text_input[0].input(CODE).run()
    assert "INTC -7.5% on Nov 13 (replay)" in _markdown(at) and "13 Nov 2026 18:00 UTC" in _captions(at)
    badges = [m.value for m in at.markdown if "badge" in m.value]
    assert len(badges) == 1 and "Replay: a demo" in badges[0]


def test_a_quote_error_shows_na(fake_api, monkeypatch):
    monkeypatch.setattr(ui_client.DeskClient, "quotes", lambda self: {"NVDA": {"ticker": "NVDA", "error": "down"}})
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    at.text_input[0].input(CODE).run()
    assert at.metric[0].value == "n/a" and "Quote unavailable" in _captions(at)


def test_not_covered_notice_is_shown_above_the_answer(fake_api, monkeypatch):
    monkeypatch.setattr(ui_client.DeskClient, "wait", lambda self, job_id, on_poll=None: {
        "status": "done", "mode": "quick", "answer": "Apple trades at $231.05 [Doc 1].", "as_of": "2026-11-13T18:01:00",
        "notice": "SEC filings for Apple (AAPL) aren't covered: the filings index has AMD, INTC and NVDA only.",
        "timings": {"market:AAPL": 1.2, "total": 4.1}, "used_tools": ["market", "news"], "sources": []})
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    at.text_input[0].input(CODE).run()
    at.text_input[0].input("What is Apple's price?")
    ask_button(at).click().run()
    values = [m.value for m in at.markdown]
    covered = next(i for i, v in enumerate(values) if "aren't covered" in v)
    answer = next(i for i, v in enumerate(values) if "Apple trades at" in v)
    assert "**📄 Not covered**" in values and covered < answer
    assert not at.warning                                    # nothing else was in the notice
    assert "Tools used: Live quote, News." in _markdown(at)  # Quick trace as text, not JSON
    assert not at.json


def test_reruns_keep_the_tab_the_answer_and_examples_only_fill(fake_api):
    """The example buttons and Refresh rerun the app; the open tab (kept in session state under "tab"),
    the last answer and the mode must survive. AppTest can't click a tab or see the scroll position,
    so the tab is set through session state, which is what the browser updates when a viewer switches."""
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    at.text_input[0].input(CODE).run()
    at.text_input[0].input("What is NVIDIA's stock price right now?")
    at.radio[0].set_value("Deep")
    ask_button(at).click().run()
    assert r"NVIDIA trades at \$176.40" in _markdown(at)
    assert at.session_state["tab"] == "Ask"   # the tabs widget owns the key (it tracks state)
    at.session_state["tab"] = "Watchlist"
    at.run()

    next(b for b in at.button if b.label == "Compare NVIDIA and AMD revenue").click().run()
    assert at.text_input[0].value == "Compare NVIDIA and AMD revenue"     # filled ...
    assert len(fake_api) == 1                                            # ... not sent
    assert at.session_state["tab"] == "Watchlist" and at.radio[0].value == "Deep"
    assert r"NVIDIA trades at \$176.40" in _markdown(at)                 # the last answer is still shown

    next(b for b in at.button if b.label == "Refresh data").click().run()
    assert at.session_state["tab"] == "Watchlist" and r"NVIDIA trades at \$176.40" in _markdown(at)
    assert [t.label for t in at.tabs] == ["Ask", "Watchlist", "Alerts", "How it works"]   # labels never change


def test_a_job_interrupted_by_a_rerun_is_resumed(fake_api):
    """A rerun mid-poll (a tab switch, an example) stops the script; the job id stays in session state,
    so the next run polls the same job instead of losing the answer."""
    at = _app({"API_URL": URL, "ACCESS_CODE": CODE})
    at.text_input[0].input(CODE).run()
    at.session_state["pending"] = {"job_id": "d" * 32, "mode": "deep", "question": "NVDA price?"}
    at.run()
    assert r"NVIDIA trades at \$176.40" in _markdown(at) and "pending" not in at.session_state
    assert fake_api == []                                     # resumed, not asked again


# ── display helpers (ui/view.py) ──

COVERED = "SEC filings for Apple (AAPL) aren't covered: the filings index has AMD, INTC and NVDA only."


@pytest.mark.parametrize("notice, expected", [
    (None, (None, None)),
    (COVERED, (COVERED, None)),
    ("Deep mode wasn't available (timeout), so this is a Quick answer. " + COVERED,
     (COVERED, "Deep mode wasn't available (timeout), so this is a Quick answer.")),
    ("Partial answer: the 75 s time budget ran out.", (None, "Partial answer: the 75 s time budget ran out.")),
])
def test_split_notice(notice, expected):
    assert view.split_notice(notice) == expected


def test_replay_and_direction():
    assert view.is_replay({"dedupe_key": "replay:price_move:INTC:2026-11-13", "title": "INTC -7.5%"})
    assert view.is_replay({"title": "INTC -7.5% on Nov 13 (replay)"})
    assert not view.is_replay({"dedupe_key": "price_move:NVDA:2026-11-13", "title": "NVDA -3.2% on Nov 13"})
    assert view.is_down({"kind": "price_move", "title": "NVDA -3.2% on Nov 13"})
    assert view.is_down({"kind": "price_below", "title": "NVDA below $150"})
    assert not view.is_down({"kind": "price_move", "title": "NVDA +3.2% on Nov 13"})


def test_group_sources_keeps_a_fixed_order_and_drops_empty_groups():
    sources = [{"kind": "news", "label": "a"}, {"kind": "market", "label": "b"}, {"kind": "x", "label": "c"},
               {"kind": "news", "label": "d"}]
    assert [(t, [s["label"] for s in items]) for t, items in view.group_sources(sources)] == [
        ("Market data", ["b"]), ("News", ["a", "d"]), ("Other", ["c"])]


def test_trace_lines_are_readable_for_quick_and_deep():
    assert view.trace_lines({"used_tools": ["market", "rag"]})[-1] == "Tools used: Live quote, SEC filings."
    deep = view.trace_lines({"trace": {"fallback": "planner timeout", "plan": {"tasks": [
        {"agent": "market"}, {"agent": "filings"}]}, "rounds": [[{"agent": "market", "seconds": 2.0, "findings": [{}],
                                                                  "gaps": [], "steps": [{"action": "tool", "tool": "get_quote"}]}]]}})
    assert deep == ["Deep mode wasn't available (planner timeout), so this is a Quick answer.",
                    "Route: the planner chose market, filings.",
                    "Round 1: **market** agent, 2.0 s, 1 finding, 0 gaps, tools: get_quote."]
    assert view.tool_timings({"timings": {"market:NVDA": 1.2, "llm": 3.0, "total": 4.5, "llm_calls": 1}}) == [
        {"Step": "Live quote (NVDA)", "Seconds": 1.2}, {"Step": "Gemini", "Seconds": 3.0}]


def test_small_formatters():
    assert view.fmt_time("2026-11-13T18:00:21") == "13 Nov 2026 18:00 UTC"
    assert view.fmt_time("bad") == "bad" and view.fmt_time(None) == ""
    assert view.progress_fraction(5, "deep") == 0.2 and view.progress_fraction(60, "quick") == 0.95
    assert view.answer_meta({"mode": "deep", "as_of": "2026-11-13T18:01:00",
                             "timings": {"total": 5.7, "llm_calls": 3}}) == \
        "Deep · as of 13 Nov 2026 18:01 UTC · 5.7 s · 3 Gemini calls"
