"""Deep mode, v2 additions on top of the ported v1 tests (test_agents.py): frozen stage 1, planted fake
number, quote/filing evidence labels, latest_filing, and the failure paths PLAN A.12 names (malformed
planner JSON, LLM timeout, 75 s budget → partial answer). Gemini is always scripted."""

from types import SimpleNamespace
from unittest import mock

import pytest

from core import llm
from core.agents import checker, deep, specialists, supervisor, tools
from core.agents.evidence import EvidenceBoard

FAR = 10**12
EVIDENCE = ("NVDA price $202.06, day change -1.26%; market cap $4,879.1B. Total revenue 57,006 35,082 "
            "(In millions). Net revenue was $7.4 billion, up 36%.")
QUOTE = {"ticker": "NVDA", "price": 202.06, "prev_close": 204.64, "open": 207.5, "high": 209.9, "low": 201.6,
         "volume": 1.2e8, "market_cap": 4.88e12, "pe_ratio": 25.5, "change_pct": -1.26,
         "fetched_at": "2026-11-05T09:00:00+00:00", "source": "finnhub", "trading_date": "2026-11-04"}


def _report(gaps=(), error=None, add_evidence=True):
    def run(agent_name, instruction, board, deadline, companies):
        if add_evidence:
            board.add(agent_name, "t", f"{agent_name} evidence $10.5B", f"{agent_name} source", "https://s")
        return specialists.Report(agent=agent_name, findings=[{"claim": "c", "evidence": ["E1"]}],
                                  gaps=list(gaps), seconds=1.0, error=error)
    return run


@pytest.fixture
def no_checker_llm():
    with mock.patch.object(checker.llm, "call", side_effect=llm.LLMUnavailableError("x")):
        yield


# ─── fact-checker ───────────────────────────────────────────────────────────

def test_checker_stage_one_is_the_frozen_module():
    from core import numbers

    assert checker.verify_numbers is numbers.verify_numbers and checker.UNVERIFIED_MARK == numbers.UNVERIFIED_MARK


def test_planted_fake_number_is_flagged_unverified():
    reply = SimpleNamespace(text='{"verdicts": [{"number": "$99.9 billion", "verdict": "unsupported"}]}')
    with mock.patch.object(checker.llm, "call", return_value=reply):
        v = checker.check("NVDA is $202.06 and data center revenue was $99.9 billion.", EVIDENCE, FAR)
    assert v.unsupported == ["$99.9 billion"]
    assert "$99.9 billion" + checker.UNVERIFIED_MARK in v.answer
    assert "$202.06" + checker.UNVERIFIED_MARK not in v.answer


# ─── tools ──────────────────────────────────────────────────────────────────

def test_quote_evidence_names_its_source_and_session():
    b = EvidenceBoard()
    with mock.patch.object(tools.market, "get_quote", return_value=QUOTE):
        tools.run_tool("get_quote", {"ticker": "NVDA"}, b, "market")
    e = b.get("E1")
    assert "session 2026-11-04, source finnhub" in e.summary
    assert e.label == "Finnhub quote: NVDA" and e.url == "https://finnhub.io/quote/NVDA"


def test_get_quotes_uses_trading_date_and_survives_missing_change():
    quotes = {"QQQ": {"ticker": "QQQ", "price": 691.95, "prev_close": 685.28, "change_pct": 0.97,
                      "trading_date": "2026-11-04", "source": "yfinance"},
              "SPY": {"ticker": "SPY", "price": 766.81, "prev_close": None, "change_pct": None,
                      "trading_date": "2026-11-04", "source": "finnhub"},
              "XYZ": {"ticker": "XYZ", "error": "No price data"}}
    b = EvidenceBoard()
    with mock.patch.object(tools.market, "get_quotes", return_value=quotes):
        out = tools.run_tool("get_quotes", {"tickers": ["QQQ", "SPY", "XYZ"]}, b, "market")
    assert "(session 2026-11-04, source yfinance)" in b.get("E1").summary
    assert "day change N/A" in b.get("E2").summary
    assert out["evidence"][2] == {"ticker": "XYZ", "error": "No price data"}


def test_search_filings_label_carries_period_and_section():
    chunk = {"company": "NVIDIA", "ticker": "NVDA", "form": "10-Q", "doc_date": "2026-08-26", "period": "2026-07-26",
             "section": "Note: Concentration of Revenue and Customers", "url": "https://sec/q", "text": "Customer A 22%"}
    b = EvidenceBoard()
    with mock.patch.object(tools.retriever, "search", return_value=[chunk]):
        tools.run_tool("search_filings", {"query": "customers"}, b, "filings")
    assert b.get("E1").label == ("NVDA: 10-Q, period 2026-07-26, filed 2026-08-26, "
                                 "Note: Concentration of Revenue and Customers")


def test_latest_filing_lists_10k_10q_and_earnings_8k(monkeypatch):
    from core import ingest

    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    filings = [ingest.Filing("NVDA", "NVIDIA", 1045810, form, f"acc-{form}", filed, period, "d.htm")
               for form, filed, period in [("10-K", "2026-02-25", "2026-01-25"), ("10-Q", "2026-08-26", "2026-07-26"),
                                           ("8-K", "2026-08-26", "2026-08-26")]]
    b = EvidenceBoard()
    nvda = tools.tickers.Company("NVDA", "NVIDIA", 1045810)
    with mock.patch.object(tools.ingest, "latest_filings", return_value=filings), \
         mock.patch.object(tools.tickers, "lookup_ticker", return_value=nvda):
        out = tools.run_tool("latest_filing", {"ticker": "NVDA"}, b, "filings")
    assert len(out["evidence"]) == 3
    assert b.get("E2").summary == "NVDA latest 10-Q: fiscal period ended 2026-07-26, filed 2026-08-26 (accession acc-10-Q)"
    assert "earnings release 8-K" in b.get("E3").summary


# ─── failure paths ──────────────────────────────────────────────────────────

def test_malformed_planner_json_falls_back_to_quick():
    with mock.patch.object(supervisor.llm, "call", return_value=SimpleNamespace(text="{not json")), \
         mock.patch.object(deep.quick_agent, "answer", return_value={"answer": "q", "mode": "quick"}):
        result = deep.answer_deep("Compare NVIDIA and AMD revenue and stock moves")
    assert result["mode"] == "quick-fallback" and "planner unavailable" in result["notice"]


def test_writer_timeout_falls_back_to_quick(no_checker_llm):
    with mock.patch.object(deep, "run_specialist", side_effect=_report()), \
         mock.patch.object(deep.supervisor, "write", side_effect=llm.LLMUnavailableError("primary: ReadTimeout")), \
         mock.patch.object(deep.quick_agent, "answer", return_value={"answer": "q", "mode": "quick"}):
        result = deep.answer_deep("What is NVIDIA's stock price?")
    assert result["mode"] == "quick-fallback" and "writer unavailable" in result["notice"]
    assert result["answer"] == "q"


def test_budget_exhaustion_gives_a_partial_answer_marked_as_such(no_checker_llm):
    """The 75 s budget runs out inside the specialists: the board's evidence is still written up."""
    run = _report(gaps=["Market Analyst stopped early: time budget used up"], error=deep.TIME_BUDGET_ERROR)
    with mock.patch.object(deep, "run_specialist", side_effect=run), \
         mock.patch.object(deep.supervisor, "write", return_value="NVDA is $10.5B [E1]."):
        result = deep.answer_deep("What is NVIDIA's stock price?")
    assert result["mode"] == "deep" and result["answer"] == "NVDA is $10.5B [E1]."
    assert result["trace"]["partial"] == ["market"] and result["notice"].startswith("Partial answer")


def test_complete_answer_has_no_partial_notice(no_checker_llm):
    with mock.patch.object(deep, "run_specialist", side_effect=_report()), \
         mock.patch.object(deep.supervisor, "write", return_value="NVDA is $10.5B [E1]."):
        result = deep.answer_deep("What is NVIDIA's stock price?")
    assert result["notice"] is None and result["trace"]["partial"] is None


def test_loop_deadline_error_matches_the_partial_marker():
    from core.agents import loop

    result = loop.run_tool_loop("market", "sys", "q", ["get_quote"], EvidenceBoard(), deadline=0)
    assert result.error == deep.TIME_BUDGET_ERROR


def test_request_id_reaches_specialist_threads(no_checker_llm):
    from core import log

    seen = []
    base = _report()

    def run(agent_name, instruction, board, deadline, companies):
        seen.append(log.get_request_id())
        return base(agent_name, instruction, board, deadline, companies)
    plan = {"companies": ["NVDA", "AMD"], "tasks": [{"agent": "filings", "instruction": "a"},
                                                    {"agent": "market", "instruction": "b"}]}
    log.new_request_id("req-deep")
    with mock.patch.object(deep.supervisor, "plan", return_value=plan), \
         mock.patch.object(deep, "run_specialist", side_effect=run), \
         mock.patch.object(deep.supervisor, "write", return_value="A [E1]."):
        deep.answer_deep("Compare NVIDIA and AMD revenue and stock moves")
    assert seen == ["req-deep", "req-deep"]
