"""Eval runner: the result contract the frozen scorer reads (from real Quick/Deep code paths with every
external call mocked), the API target, and report sections."""

from types import SimpleNamespace
from unittest import mock

from core import agent, llm
from core.agents import checker, deep, specialists
from core.tickers import Company
from evals import run, scoring

NVDA = Company("NVDA", "NVIDIA", 1045810)
QUOTE = {"ticker": "NVDA", "price": 200.0, "prev_close": 198.0, "change_pct": 1.01, "trading_date": "2026-11-04",
         "source": "yfinance", "fetched_at": "2026-11-05T08:00:00+00:00"}
ITEM = {"id": "s1", "type": "single", "q": "What is NVIDIA's stock price?", "companies": ["NVDA"], "agents": ["market"]}


def _assert_contract(result: dict, mode: str):
    for key in run.RESULT_KEYS:
        assert key in result, key
    assert all("ticker" in c for c in result["companies"])
    if mode == "deep":
        for key in run.DEEP_TRACE_KEYS:
            assert key in result["trace"], key
        # The checker didn't run on a Quick fallback (None); the scorer reads it with `or {}`.
        assert result["trace"]["checker"] is None or "unsupported" in result["trace"]["checker"]
    s = scoring.score(ITEM, mode, {**result, "_seconds": 1.0, "_llm_calls": 1})
    assert s["ok"] and s["companies_ok"] and s["agents_ok"]
    return s


def test_quick_result_meets_the_scorer_contract():
    with mock.patch.object(agent.tickers, "find_companies", return_value=[NVDA]), \
         mock.patch.object(agent.market, "get_quote", return_value=QUOTE), \
         mock.patch.object(agent.llm, "generate", return_value="NVDA is $200.00 [Doc 1]."):
        result = agent.answer(ITEM["q"])
    s = _assert_contract(result, "quick")
    assert s["unmatched"] == 0 and s["numbers"] == 1  # the cited number traces to the context


def test_deep_result_meets_the_scorer_contract():
    def run_specialist(agent_name, instruction, board, deadline, companies):
        board.add(agent_name, "get_quote", "NVDA price $200.00", "quote", "https://q", "market")
        return specialists.Report(agent=agent_name, findings=[{"claim": "price", "evidence": ["E1"]}], seconds=1.0)
    with mock.patch.object(deep.tickers, "find_companies", return_value=[NVDA]), \
         mock.patch.object(deep, "run_specialist", side_effect=run_specialist), \
         mock.patch.object(deep.supervisor, "write", return_value="NVDA is $200.00 [E1]."), \
         mock.patch.object(checker.llm, "call", side_effect=llm.LLMUnavailableError("x")):
        result = deep.answer_deep(ITEM["q"])
    s = _assert_contract(result, "deep")
    assert s["cited_lines"] == s["numeric_lines"] == 0 or s["cited_lines"] == s["numeric_lines"]


def test_quick_fallback_result_also_meets_the_contract():
    quick = {"answer": "NVDA $200.00 [Doc 1]", "error": None, "used_tools": ["market"],
             "companies": [NVDA.to_dict()], "mode": "quick", "context": "[Doc 1] NVDA price $200.00"}
    with mock.patch.object(deep.supervisor, "plan", side_effect=llm.LLMUnavailableError("503")), \
         mock.patch.object(deep.quick_agent, "answer", return_value=quick):
        result = deep.answer_deep("Why did NVIDIA move today?")
    assert result["mode"] == "quick-fallback" and result["trace"]["fallback"]
    _assert_contract(result, "deep")


# ─── API target ─────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, polls):
        self.polls, self.posted = list(polls), None

    def post(self, url, json, headers, timeout):
        self.posted = {"url": url, "json": json, "headers": headers}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"job_id": "j1", "poll": "/ask/j1"})

    def get(self, url, headers, timeout):
        body = self.polls.pop(0)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


def test_api_target_requests_context_and_polls_until_done():
    done = {"status": "done", "answer": "A [E1]", "context": "E1 ...", "mode": "deep", "used_tools": ["market"],
            "companies": [{"ticker": "NVDA"}], "trace": {"rounds": [[]], "checker": {"unsupported": []}, "fallback": None},
            "timings": {"llm_calls": 5}}
    session = FakeSession([{"status": "pending"}, done])
    r = run.run_api("deep", "q", "https://api.example/", "code", session=session, sleep=lambda s: None)
    assert session.posted["json"] == {"question": "q", "mode": "deep", "include_context": True}
    assert session.posted["url"] == "https://api.example/ask" and session.posted["headers"]["x-access-code"] == "code"
    assert r["answer"] == "A [E1]" and r["context"] == "E1 ..." and r["_llm_calls"] == 5


def test_api_target_records_failures_instead_of_raising():
    session = FakeSession([{"status": "error", "error": "Deep mode failed"}])
    r = run.run_api("quick", "q", "https://api.example", "code", session=session, sleep=lambda s: None)
    assert r["answer"] is None and r["error"] == "Deep mode failed"


# ─── report ─────────────────────────────────────────────────────────────────

def test_report_sections_are_replaced_independently(tmp_path):
    path = tmp_path / "v2-results.md"
    run.write_report("local", "local v1", path)
    run.write_report("api", "deployed v1", path)
    run.write_report("local", "local v2", path)
    text = path.read_text(encoding="utf-8")
    assert "local v2" in text and "local v1" not in text and "deployed v1" in text
    assert text.index("## Local") < text.index("## Deployed")
    assert text.count("Known quirks of the frozen scorer (D8)") == 1 and "Billion" in text   # review A, S5


def test_render_section_compares_with_the_v1_baseline():
    items = [ITEM]
    result = {"answer": "NVDA is $200.00 [Doc 1].", "context": "[Doc 1] NVDA price $200.00", "used_tools": ["market"],
              "companies": [{"ticker": "NVDA"}], "mode": "quick", "error": None, "_seconds": 2.0, "_llm_calls": 1}
    md = run.render_section("local", items, {"s1:quick": result}, {"s1:quick": result},
                            {"date": "2026-11-05", "answer_dates": "2026-11-05", "models": "m", "kb": "kb"})
    assert "| Right tools/agents used (routing) | 100% | **100%** |" in md
    assert "| single (1) | quick | 0/1 (0%) → 0/1 (0%)" in md
