"""Multi-agent package: evidence board, tools, tool loop, specialists, supervisor,
fact-checker and the deep orchestrator. Gemini is always scripted (no network)."""

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from core import llm
from core.agents import checker, deep, loop, specialists, supervisor, tools
from core.agents.evidence import EvidenceBoard

FAR = 10**12  # a deadline that never hits


# ─── fakes ───────────────────────────────────────────────────────────────────

def fn_call(name, **args):
    return SimpleNamespace(name=name, args=args)


def response(text=None, calls=None):
    return SimpleNamespace(text=text, function_calls=calls or None,
                           candidates=[SimpleNamespace(content=f"model-turn:{text or calls}")])


QUOTE = {"ticker": "NVDA", "price": 202.06, "prev_close": 204.64, "open": 207.5, "high": 209.9, "low": 201.6,
         "volume": 1.2e8, "market_cap": 4.88e12, "pe_ratio": 25.5, "change_pct": -1.26,
         "fetched_at": "2026-11-03T09:00:00+00:00"}


# ─── evidence board ──────────────────────────────────────────────────────────

def test_evidence_ids_prompt_and_sources():
    b = EvidenceBoard()
    e1 = b.add("market", "get_quote", "NVDA price $202.06", "Yahoo: NVDA", "https://y", "market")
    e2 = b.add("news", "get_news", "headline", "Nvidia rallies", None, "news")
    assert (e1.id, e2.id, len(b)) == ("E1", "E2", 2)
    assert b.for_prompt(["E2"]).startswith("[E2] (news/get_news")
    sources = b.sources(cited={"E2"})
    assert [s["id"] for s in sources] == ["E2", "E1"] and sources[0]["cited"] and not sources[1]["cited"]
    assert "$202.06" in b.text()


# ─── tools ───────────────────────────────────────────────────────────────────

def test_get_quote_tool_writes_evidence():
    b = EvidenceBoard()
    with mock.patch.object(tools.market, "get_quote", return_value=QUOTE):
        out = tools.run_tool("get_quote", {"ticker": "nvda"}, b, "market")
    assert out["evidence"][0]["id"] == "E1"
    assert "price $202.06" in b.get("E1").summary and "-1.26%" in b.get("E1").summary
    assert b.get("E1").url == "https://finance.yahoo.com/quote/NVDA"


def test_tool_errors_become_messages_for_the_model():
    b = EvidenceBoard()
    with mock.patch.object(tools.market, "get_quote", side_effect=tools.market.MarketDataError("Yahoo down")):
        assert tools.run_tool("get_quote", {"ticker": "X"}, b, "market") == {"error": "Yahoo down"}
    with mock.patch.object(tools.retriever, "search", side_effect=RuntimeError("secret detail")):
        out = tools.run_tool("search_filings", {"query": "q"}, b, "filings")
    assert "secret" not in out["error"]
    assert tools.run_tool("nope", {}, b, "x") == {"error": "Unknown tool nope"}
    assert len(b) == 0


def test_latest_filing_needs_sec_access():
    out = tools.run_tool("latest_filing", {"ticker": "NVDA"}, EvidenceBoard(), "filings")
    assert "not configured" in out["error"]


def test_search_filings_evidence_has_label_and_link():
    chunk = {"company": "NVIDIA", "source": "10-Q 0001", "form": "10-Q", "doc_date": "2026-08-27",
             "url": "https://sec/x", "text": "Revenue was $46.7 billion.", "score": 0.8}
    b = EvidenceBoard()
    with mock.patch.object(tools.retriever, "search", return_value=[chunk]) as search:
        tools.run_tool("search_filings", {"query": "revenue", "ticker": "nvda"}, b, "filings")
    assert search.call_args.kwargs["ticker"] == "NVDA"
    e = b.get("E1")
    assert e.label == "NVIDIA: 10-Q, filed 2026-08-27" and e.url == "https://sec/x" and e.kind == "document"


# ─── tool loop ───────────────────────────────────────────────────────────────

def test_loop_runs_tools_then_answers():
    b = EvidenceBoard()
    script = [response(calls=[fn_call("get_quote", ticker="NVDA")]), response(text="FINDINGS:\n- NVDA $202.06 [E1]")]
    with mock.patch.object(loop.llm, "call", side_effect=script) as call, \
         mock.patch.object(tools.market, "get_quote", return_value=QUOTE):
        result = loop.run_tool_loop("market", "sys", "NVDA price?", ["get_quote"], b, FAR)
    assert result.text.startswith("FINDINGS") and result.llm_calls == 2
    assert [s["action"] for s in result.steps] == ["tool", "answer"]
    assert result.steps[0]["evidence"] == ["E1"] and result.steps[0]["args"] == {"ticker": "NVDA"}
    # the second call carries the model turn + the function response
    second_contents = call.call_args_list[1].args[0]
    assert len(second_contents) == 3


def test_loop_withdraws_tools_after_the_step_limit():
    def fake_call(contents, config, **kw):
        if config.tools:
            return response(calls=[fn_call("get_quote", ticker="NVDA")])
        return response(text="final")
    with mock.patch.object(loop.llm, "call", side_effect=fake_call), \
         mock.patch.object(tools.market, "get_quote", return_value=QUOTE):
        result = loop.run_tool_loop("market", "sys", "q", ["get_quote"], EvidenceBoard(), FAR, max_steps=2)
    assert result.text == "final"
    assert [s["action"] for s in result.steps] == ["tool", "tool", "answer"]


def test_loop_reports_model_outage_and_deadline():
    with mock.patch.object(loop.llm, "call", side_effect=llm.LLMUnavailableError("503")):
        result = loop.run_tool_loop("market", "sys", "q", ["get_quote"], EvidenceBoard(), FAR)
    assert result.text is None and "unavailable" in result.error
    result = loop.run_tool_loop("market", "sys", "q", ["get_quote"], EvidenceBoard(), deadline=0)
    assert result.error == "time budget used up" and result.llm_calls == 0


# ─── specialists ─────────────────────────────────────────────────────────────

def test_parse_report_reads_findings_gaps_and_ids():
    text = ("Some preamble\n**FINDINGS:**\n- NVIDIA revenue was $57.0B [E4]\n* AMD fell 7.5% [E2, E3]\n"
            "GAPS:\n- AMD Q3 revenue not found\n")
    findings, gaps = specialists.parse_report("filings", text)
    assert findings == [{"claim": "NVIDIA revenue was $57.0B", "evidence": ["E4"]},
                        {"claim": "AMD fell 7.5%", "evidence": ["E2", "E3"]}]
    assert gaps == ["AMD Q3 revenue not found"]
    assert specialists.parse_report("x", "FINDINGS:\n- a [E1]\nGAPS:\n- none")[1] == []


def test_specialist_uses_only_its_tools_and_falls_back_to_raw_evidence():
    board = EvidenceBoard()
    board.add("market", "get_quote", "NVDA price $202.06", "Yahoo: NVDA")
    fake = loop.LoopResult(text=None, llm_calls=3, error="time budget used up")
    with mock.patch.object(specialists, "run_tool_loop", return_value=fake) as run:
        report = specialists.run_specialist("market", "price?", board, FAR, ["NVDA"])
    assert run.call_args.args[3] == ["get_quote", "get_quotes"]
    assert "Companies (tickers): NVDA" in run.call_args.args[2]
    assert report.findings == [{"claim": "NVDA price $202.06", "evidence": ["E1"]}]
    assert report.gaps == ["Market Analyst could not complete its search"]  # review A, S1: no internal detail


# ─── supervisor ──────────────────────────────────────────────────────────────

def test_clean_tasks_merges_duplicates_and_drops_unknown_agents():
    tasks = supervisor._clean_tasks([
        {"agent": "filings", "instruction": "NVDA revenue"}, {"agent": "filings", "instruction": "AMD revenue"},
        {"agent": "astrologer", "instruction": "stars"}, {"agent": "market", "instruction": ""}])
    assert tasks == [{"agent": "filings", "instruction": "NVDA revenue Also: AMD revenue"}]


def test_plan_normalises_companies_and_never_returns_empty_work():
    with mock.patch.object(supervisor, "_json_call", return_value={"companies": ["nvda", "amd"], "tasks": []}):
        plan = supervisor.plan("Compare them", ["NVDA"], FAR)
    assert plan == {"companies": ["NVDA", "AMD"], "tasks": [{"agent": "filings", "instruction": "Compare them"}]}


def test_follow_up_shows_what_was_already_tried():
    reports = [{"agent": "filings", "findings": [], "gaps": ["AMD Q3 missing"],
                "steps": [{"action": "tool", "tool": "search_filings", "args": {"query": "AMD Q3 revenue"}}]}]
    with mock.patch.object(supervisor, "_json_call", return_value={"tasks": []}) as jc:
        assert supervisor.follow_up("q", reports, FAR) == []
    assert 'search_filings({"query": "AMD Q3 revenue"})' in jc.call_args.args[1]


# ─── fact-checker ────────────────────────────────────────────────────────────

EVIDENCE = ("NVDA price $202.06, day change -1.26%; market cap $4,879.1B. Total revenue 57,006 35,082 "
            "(In millions). Net revenue was $7.4 billion, up 36%.")


@pytest.mark.parametrize("answer, unmatched", [
    ("Revenue was $57,006 million [E4]", []),            # table figure "in millions"
    ("Revenue $57.0B and $35.1 billion a year ago", []),  # unit conversion + rounding
    ("price $202 and -1.3%", []),                         # display precision
    ("market cap $4.88 trillion", []),
    ("guidance of $68 billion", ["$68 billion"]),         # invented
    ("up 62% year over year", ["62%"]),                    # not in evidence (derived or not: LLM decides)
    ("In 2025, Q3 FY26 had 3 segments; 10-Q [E12] filed 2026-11-02", []),  # not checkable numbers
])
def test_verify_numbers(answer, unmatched):
    _, bad = checker.verify_numbers(answer, EVIDENCE)
    assert [n.raw.strip() for n in bad] == unmatched


def test_check_flags_unsupported_and_accepts_derived():
    draft = "Revenue rose 62% to $57.0B, with guidance of $68 billion."
    reply = SimpleNamespace(text=json.dumps({"verdicts": [
        {"number": "62%", "verdict": "derived"}, {"number": "$68 billion", "verdict": "unsupported"}]}))
    with mock.patch.object(checker.llm, "call", return_value=reply):
        v = checker.check(draft, EVIDENCE, FAR)
    assert v.derived == ["62%"] and v.unsupported == ["$68 billion"] and v.llm_calls == 1
    assert v.answer == "Revenue rose 62% to $57.0B, with guidance of $68 billion" + checker.UNVERIFIED_MARK + "."


def test_check_without_llm_flags_every_unmatched_number():
    with mock.patch.object(checker.llm, "call", side_effect=llm.LLMUnavailableError("down")):
        v = checker.check("Revenue rose 62%.", EVIDENCE, FAR)
    assert v.unsupported == ["62%"] and checker.UNVERIFIED_MARK in v.answer


def test_check_with_everything_supported_makes_no_llm_call():
    with mock.patch.object(checker.llm, "call") as call:
        v = checker.check("NVDA is $202.06 (-1.26%).", EVIDENCE, FAR)
    call.assert_not_called()
    assert (v.numbers_checked, v.supported, v.unsupported) == (2, 2, [])


# ─── deep orchestrator ───────────────────────────────────────────────────────

def _report(agent, gaps=(), add_evidence=True):
    def run(agent_name, instruction, board, deadline, companies):
        if add_evidence:
            board.add(agent_name, "t", f"{agent_name} evidence $10.5B", f"{agent_name} source", "https://s")
        return specialists.Report(agent=agent_name, findings=[{"claim": "c", "evidence": ["E1"]}],
                                  gaps=list(gaps), seconds=1.0)
    return run


@pytest.fixture
def no_checker_llm():
    with mock.patch.object(checker.llm, "call", side_effect=llm.LLMUnavailableError("x")):
        yield


def test_fast_path_skips_the_planner(no_checker_llm):
    with mock.patch.object(deep.supervisor, "plan") as plan, \
         mock.patch.object(deep, "run_specialist", side_effect=_report("market")) as run, \
         mock.patch.object(deep.supervisor, "write", return_value="NVDA is $10.5B [E1]."):
        result = deep.answer_deep("What is NVIDIA's stock price?")
    plan.assert_not_called()
    assert run.call_args.args[0] == "market"
    assert result["mode"] == "deep" and result["trace"]["fast_path"]
    assert result["sources"][0]["cited"] and result["answer"] == "NVDA is $10.5B [E1]."


def test_full_path_plans_dispatches_in_parallel_and_follows_up_once(no_checker_llm):
    plan = {"companies": ["NVDA", "AMD"], "tasks": [{"agent": "filings", "instruction": "rev"},
                                                    {"agent": "market", "instruction": "moves"}]}
    with mock.patch.object(deep.supervisor, "plan", return_value=plan), \
         mock.patch.object(deep, "run_specialist", side_effect=_report("x", gaps=["AMD Q3 missing"])) as run, \
         mock.patch.object(deep.supervisor, "follow_up", return_value=[{"agent": "news", "instruction": "x"}]) as fu, \
         mock.patch.object(deep.supervisor, "write", return_value="Answer [E1]."):
        result = deep.answer_deep("Compare NVIDIA and AMD revenue and stock moves")
    assert [c.args[0] for c in run.call_args_list] == ["filings", "market", "news"]
    fu.assert_called_once()
    assert [len(r) for r in result["trace"]["rounds"]] == [2, 1]
    assert result["used_tools"] == ["filings", "market"]
    assert [c["ticker"] for c in result["companies"]] == ["NVDA", "AMD"]


def test_planner_outage_falls_back_to_quick():
    quick = {"answer": "quick answer", "error": None, "mode": "quick"}
    with mock.patch.object(deep.supervisor, "plan", side_effect=llm.LLMUnavailableError("503")), \
         mock.patch.object(deep.quick_agent, "answer", return_value=dict(quick)):
        result = deep.answer_deep("Compare NVIDIA and AMD revenue and stock moves")
    assert result["mode"] == "quick-fallback" and result["answer"] == "quick answer"
    assert "planner unavailable" in result["notice"]


def test_no_evidence_falls_back_to_quick():
    with mock.patch.object(deep, "run_specialist", side_effect=_report("market", add_evidence=False)), \
         mock.patch.object(deep.quick_agent, "answer", return_value={"answer": "q", "mode": "quick"}):
        result = deep.answer_deep("What is NVIDIA's stock price?")
    assert result["mode"] == "quick-fallback"


def test_daily_cap_falls_back_to_quick(monkeypatch, local_store):
    monkeypatch.setattr(deep, "DEEP_MODE_DAILY_CAP", 1)
    with mock.patch.object(deep, "run_specialist", side_effect=_report("market")), \
         mock.patch.object(deep.supervisor, "write", return_value="A [E1]."), \
         mock.patch.object(deep.checker.llm, "call", side_effect=llm.LLMUnavailableError("x")), \
         mock.patch.object(deep.quick_agent, "answer", return_value={"answer": "q", "mode": "quick"}):
        assert deep.answer_deep("What is NVIDIA's stock price?")["mode"] == "deep"
        second = deep.answer_deep("What is NVIDIA's stock price?")
    assert second["mode"] == "quick-fallback" and "daily limit" in second["notice"]


def test_empty_question_is_rejected():
    with pytest.raises(ValueError):
        deep.answer_deep("  ")


def test_why_questions_never_take_the_fast_path(no_checker_llm):
    plan = {"companies": ["AMD"], "tasks": [{"agent": "market", "instruction": "x"},
                                            {"agent": "news", "instruction": "y"}]}
    with mock.patch.object(deep.supervisor, "plan", return_value=plan) as planner, \
         mock.patch.object(deep, "run_specialist", side_effect=_report("x")), \
         mock.patch.object(deep.supervisor, "write", return_value="A [E1]."):
        result = deep.answer_deep("Why did AMD stock fall?")
    planner.assert_called_once()
    assert not result["trace"]["fast_path"]
