"""Deep mode, v2 additions on top of the ported v1 tests (test_agents.py): frozen stage 1, planted fake
number, quote/filing evidence labels, latest_filing, and the failure paths PLAN A.12 names (malformed
planner JSON, LLM timeout, 75 s budget → partial answer). Gemini is always scripted."""

from types import SimpleNamespace
from unittest import mock

import pytest

from core import llm
from core.agents import checker, deep, loop, specialists, supervisor, tools
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
    filings = [ingest.Filing("NVDA", "NVIDIA", 1045810, form, f"acc-{form}", filed, period, "d.htm",
                             event_date="2026-08-26" if form == "8-K" else "")
               for form, filed, period in [("10-K", "2026-02-25", "2026-01-25"), ("10-Q", "2026-08-26", "2026-07-26"),
                                           ("8-K", "2026-08-26", "2026-07-26")]]
    b = EvidenceBoard()
    nvda = tools.tickers.Company("NVDA", "NVIDIA", 1045810)
    with mock.patch.object(tools.ingest, "latest_filings", return_value=filings), \
         mock.patch.object(tools.tickers, "lookup_ticker", return_value=nvda):
        out = tools.run_tool("latest_filing", {"ticker": "NVDA"}, b, "filings")
    assert len(out["evidence"]) == 3
    assert b.get("E2").summary == "NVDA latest 10-Q: fiscal period ended 2026-07-26, filed 2026-08-26 (accession acc-10-Q)"
    assert "earnings release 8-K" in b.get("E3").summary


def test_earnings_8k_never_renders_its_event_date_as_a_fiscal_period(monkeypatch):
    """Review A, M1: an 8-K's SEC reportDate is the event date, not the quarter's end."""
    from core import ingest

    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    nvda = tools.tickers.Company("NVDA", "NVIDIA", 1045810)
    eight_k = [ingest.Filing("NVDA", "NVIDIA", 1045810, "8-K", "acc-8k", "2026-08-26", "2026-07-26", "e.htm",
                             event_date="2026-08-26"),
               ingest.Filing("AMD", "AMD", 2488, "8-K", "acc-amd", "2026-11-03", "", "e.htm", event_date="2026-11-03")]
    expected = ["results are for the period ending 2026-07-26", "fiscal period is stated in the release"]
    for f, covers in zip(eight_k, expected):
        b = EvidenceBoard()
        with mock.patch.object(tools.ingest, "latest_filings", return_value=[f]), \
             mock.patch.object(tools.tickers, "lookup_ticker", return_value=nvda):
            tools.run_tool("latest_filing", {"ticker": "NVDA"}, b, "filings")
        summary = b.get("E1").summary
        assert "fiscal period ended" not in summary and f"event date {f.event_date}" in summary
        assert covers in summary
        assert "period 2026-08-26" not in f.label and "period 2026-11-03" not in f.label
    assert b.get("E1").label == "SEC NVDA 8-K 2026-11-03"   # NIT: the ticker is in the source label
    # Quick [Doc N] labels and Deep search_filings labels of an 8-K chunk
    chunk = {"ticker": "NVDA", "company": "NVIDIA", "form": "8-K", "period": "2026-07-26", "event_date": "2026-08-26",
             "doc_date": "2026-08-26", "section": "Earnings press release (EX-99.1)", "text": "Revenue $46.7 billion"}
    from core import agent as quick
    assert quick.chunk_label(chunk) == ("NVDA | 8-K | event date 2026-08-26, reports the period ending 2026-07-26 | "
                                        "filed 2026-08-26 | Earnings press release (EX-99.1)")
    b = EvidenceBoard()
    with mock.patch.object(tools.retriever, "search", return_value=[chunk]):
        tools.run_tool("search_filings", {"query": "revenue"}, b, "filings")
    assert "event date 2026-08-26, reports the period ending 2026-07-26" in b.get("E1").label
    assert "period 2026-08-26" not in b.get("E1").label


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


# ─── review A, S1: internal errors never reach the writer ──────────────────

def test_model_error_stays_in_the_report_and_the_writer_gets_a_neutral_gap():
    board = EvidenceBoard()
    board.add("filings", "latest_filing", "AMD latest 10-K: fiscal period ended 2025-12-27", "SEC AMD 10-K")
    outage = loop.LoopResult(text=None, llm_calls=1,
                             error="model unavailable (gemini-3.8-flash: HTTP 503)")
    with mock.patch.object(specialists, "run_tool_loop", return_value=outage):
        report = specialists.run_specialist("filings", "AMD revenue", board, FAR, ["AMD"])
    assert report.error == "model unavailable (gemini-3.8-flash: HTTP 503)"   # kept for the trace
    assert report.gaps == ["Filings Analyst could not complete its search"]
    with mock.patch.object(supervisor.llm, "call", return_value=SimpleNamespace(text="AMD: no data [Findings].")) as call:
        text = supervisor.write("AMD revenue?", [report.to_dict()], board, FAR)
    prompt = call.call_args.args[0]
    assert "503" not in prompt and "gemini" not in prompt.lower() and "could not complete its search" in prompt
    assert text == "AMD: no data."   # fake [Findings] citation removed


def test_fake_citation_cleanup_keeps_evidence_ids_and_links():
    raw = "Revenue $13.4B [E3]. Gap noted [findings]. See [Findings](https://x) and [Specialist]."
    with mock.patch.object(supervisor.llm, "call", return_value=SimpleNamespace(text=raw)):
        text = supervisor.write("q", [], EvidenceBoard(), FAR)
    assert text == "Revenue $13.4B [E3]. Gap noted. See [Findings](https://x) and."


# ─── review A, S2: per-company filing search; numbers cited to metadata ─────

def test_filings_instruction_asks_for_each_company_when_there_are_several():
    fake = loop.LoopResult(text="FINDINGS:\n- x [E1]")
    with mock.patch.object(specialists, "run_tool_loop", return_value=fake) as run:
        specialists.run_specialist("filings", "Compare revenue", EvidenceBoard(), FAR, ["NVDA", "AMD", "INTC"])
        several = run.call_args.args[2]
        specialists.run_specialist("filings", "NVDA revenue", EvidenceBoard(), FAR, ["NVDA"])
        one = run.call_args.args[2]
        specialists.run_specialist("market", "Compare prices", EvidenceBoard(), FAR, ["NVDA", "AMD"])
        market = run.call_args.args[2]
    assert "search_filings once per ticker (NVDA, AMD, INTC)" in several
    assert "once per ticker" not in one and "once per ticker" not in market


META = {"E4": "AMD latest 10-K: fiscal period ended 2025-12-27, filed 2026-02-04 (accession 0000002488-26-000018)"}
BOARD_TEXT = META["E4"] + "\nAMD 10-K MD&A: net revenue was $34,639 million; data center $16,635 million"


def test_number_cited_only_to_latest_filing_metadata_is_flagged():
    """The c1 case: '$23,302 million [E4]' where E4 is a latest_filing line. Even a figure that IS
    elsewhere on the board is flagged when its citation points at metadata."""
    draft = ("AMD revenue was $23,302 million [E4].\n"
             "AMD data center was $16,635 million [E4].\n"
             "AMD net revenue was $34,639 million [E5].\n"
             "The 10-K was filed 2026-02-04 [E4].")
    meta = {"E4": META["E4"]}
    with mock.patch.object(checker.llm, "call", return_value=SimpleNamespace(
            text='{"verdicts": [{"number": "$23,302 million", "verdict": "derived"}]}')):
        v = checker.check(draft, BOARD_TEXT, FAR, metadata=meta)
    assert v.unsupported == ["$23,302 million", "$16,635 million"]      # stage 2 can't rescue a mis-citation
    assert "$34,639 million [E5]" in v.answer and "$34,639 million" + checker.UNVERIFIED_MARK not in v.answer
    assert "$16,635 million" + checker.UNVERIFIED_MARK + " [E4]" in v.answer
    assert v.numbers_checked == 3 and v.supported == 1                    # the date is not a checked number


def test_deep_passes_latest_filing_items_to_the_checker(no_checker_llm):
    def run(agent_name, instruction, board, deadline, companies):
        board.add(agent_name, "latest_filing", META["E4"], "SEC AMD 10-K", "https://s")
        board.add(agent_name, "search_filings", "AMD revenue was $6,819 million", "AMD 10-Q", "https://s")
        return specialists.Report(agent=agent_name, findings=[{"claim": "c", "evidence": ["E1"]}])
    with mock.patch.object(deep, "run_specialist", side_effect=run), \
         mock.patch.object(deep.supervisor, "write", return_value="AMD revenue was $6,819 million [E1]."):
        result = deep.answer_deep("What was AMD's revenue?")
    assert result["answer"] == "AMD revenue was $6,819 million" + checker.UNVERIFIED_MARK + " [E1]."


# ─── review A, S5: capitalised scale words ─────────────────────────────────

def test_capitalised_scale_words_match_and_flags_keep_the_original_text():
    evidence = "NVDA market cap $4,832.5B; AMD market cap $309.7B"
    v = checker.check("| NVDA | $4,832.5 Billion [E1] |\n| AMD | $309.7 BILLION [E2] |", evidence, FAR, use_llm=False)
    assert v.supported == 2 and v.unsupported == []
    v = checker.check("Revenue was $99.9 Billion [E1].", evidence, FAR, use_llm=False)
    assert v.answer == "Revenue was $99.9 Billion" + checker.UNVERIFIED_MARK + " [E1]."
    assert v.unsupported == ["$99.9 Billion"]
    # Upper-case unit letters still parse (a plain answer.lower() would break these).
    assert checker.check("Market cap $4,832.5B and $309.7B.", evidence, FAR, use_llm=False).supported == 2
    assert checker.normalise_scale_words("$1.2 Million, 5 Percent, 3 Bn") == "$1.2 million, 5 percent, 3 bn"


# ─── review A, S3: the budget bounds tool execution ────────────────────────

def test_a_hanging_tool_times_out_at_the_deadline_and_is_not_joined(monkeypatch):
    import threading
    import time as _time

    release = threading.Event()
    monkeypatch.setattr(loop, "MIN_SECONDS_FOR_A_CALL", 0.1)
    monkeypatch.setitem(tools._IMPL, "get_quote", lambda b, a, args: release.wait(10) or {"evidence": []})
    started = _time.monotonic()
    with mock.patch.object(loop.llm, "call", return_value=response_calls("get_quote", ticker="NVDA")):
        result = loop.run_tool_loop("market", "sys", "q", ["get_quote"], EvidenceBoard(), started + 0.5)
    elapsed = _time.monotonic() - started
    release.set()
    assert result.steps[0]["error"] == "timed out" and result.error == deep.TIME_BUDGET_ERROR
    assert elapsed < 2


def response_calls(name, **args):
    call = SimpleNamespace(name=name, args=args)
    return SimpleNamespace(text=None, function_calls=[call], candidates=[SimpleNamespace(content="model-turn")])


def test_a_specialist_that_overruns_is_cut_off_with_its_evidence(monkeypatch):
    import threading
    import time as _time

    release = threading.Event()
    monkeypatch.setattr(deep, "WRITE_RESERVE_SECONDS", 0)
    monkeypatch.setattr(deep, "SPECIALIST_GRACE_SECONDS", 0.2)

    def slow(agent_name, instruction, board, deadline, companies):
        board.add(agent_name, "get_quote", "NVDA price $202.06", "Yahoo: NVDA")
        release.wait(10)
        return specialists.Report(agent=agent_name)
    board = EvidenceBoard()
    started = _time.monotonic()
    with mock.patch.object(deep, "run_specialist", side_effect=slow):
        reports = deep._dispatch([{"agent": "market", "instruction": "q"}], board, started + 0.3, ["NVDA"])
    elapsed = _time.monotonic() - started
    release.set()
    assert elapsed < 2
    assert reports[0]["error"] == deep.TIME_BUDGET_ERROR
    assert reports[0]["findings"] == [{"claim": "NVDA price $202.06", "evidence": ["E1"]}]
    assert reports[0]["gaps"] == ["Market Analyst could not complete its search"]


def test_finnhub_optional_calls_are_skipped_when_little_time_is_left(monkeypatch):
    import time as _time

    from core import market

    paths = []

    def get(path, **params):
        paths.append(path)
        return {"c": 200.0, "pc": 198.0, "t": 1762300800} if path == "quote" else {"marketCapitalization": 1000.0}
    monkeypatch.setattr(market, "_finnhub_get", get)
    with market.deadline(_time.monotonic() + 3):
        q = market._finnhub_detail("NVDA")
    assert paths == ["quote"] and q["market_cap"] is None and q["price"] == 200.0
    paths.clear()
    market._finnhub_detail("NVDA")   # no deadline: everything as before
    assert paths == ["quote", "stock/profile2", "stock/metric"]
    assert market.seconds_left() is None


def test_run_tool_passes_the_deadline_to_market_data():
    import time as _time

    seen = []

    def quote(ticker):
        from core import market
        seen.append(market.seconds_left())
        return QUOTE
    with mock.patch.object(tools.market, "get_quote", side_effect=quote):
        tools.run_tool("get_quote", {"ticker": "NVDA"}, EvidenceBoard(), "market", _time.monotonic() + 30)
        tools.run_tool("get_quote", {"ticker": "NVDA"}, EvidenceBoard(), "market")
    assert 25 < seen[0] <= 30 and seen[1] is None


# ─── review A NITs ──────────────────────────────────────────────────────────

def test_trace_names_follow_up_agents_that_used_tools_leaves_out(no_checker_llm):
    plan = {"companies": ["NVDA", "AMD"], "tasks": [{"agent": "filings", "instruction": "a"}]}
    with mock.patch.object(deep.supervisor, "plan", return_value=plan), \
         mock.patch.object(deep, "run_specialist", side_effect=_report(gaps=["AMD missing"])), \
         mock.patch.object(deep.supervisor, "follow_up", return_value=[{"agent": "news", "instruction": "b"}]), \
         mock.patch.object(deep.supervisor, "write", return_value="A [E1]."):
        result = deep.answer_deep("Compare NVIDIA and AMD revenue and stock moves")
    assert result["used_tools"] == ["filings"]                     # unchanged for the frozen scorer
    assert result["trace"]["follow_up_agents"] == ["news"] and "rounds[1]" in result["trace"]["note"]


# ── I2 prompt fixes: latest period when the asked one isn't filed; no batch quote before get_quote ──

def test_get_quotes_description_matches_what_it_returns():
    """The batch tool's evidence has no market cap or P/E, and its description must say so."""
    desc = tools.DECLARATIONS["get_quotes"].description
    assert "NO market cap" in desc and "NO P/E" in desc and "get_quote" in desc
    b = EvidenceBoard()
    with mock.patch.object(tools.market, "get_quotes", return_value={"NVDA": QUOTE}):
        tools.run_tool("get_quotes", {"tickers": ["NVDA"]}, b, "market")
    summary = b.get("E1").summary.lower()
    assert "market cap" not in summary and "p/e" not in summary and "202.06" in summary


def test_market_prompt_sends_valuation_questions_to_get_quote():
    system = specialists.SPECIALISTS["market"]["system"]
    assert "Use get_quotes for several tickers" not in system
    assert "if market cap or P/E is asked for, call get_quote for each ticker" in system


def test_filings_prompt_reports_the_latest_period_and_keeps_partial_evidence():
    system = specialists.SPECIALISTS["filings"]["system"]
    assert "report the latest period you did find" in system
    assert "requested period is not filed yet" in system
    assert "even if it comes from a different period" in system
    # Second round (I2): the mixed query "Q3 revenue ... customers concentration" missed the latest results.
    assert "one company and one topic per query" in system
    assert "search again for that company's latest results without naming a period" in system
    # Searches go out together, so the extra search fits in loop.MAX_STEPS.
    assert "Send independent searches together in the same step" in system
    # A general rule: no company, ticker or specific quarter in the prompt.
    for word in ("NVIDIA", "NVDA", "AMD", "Intel", "INTC", "Q3", "FY2027"):
        assert word not in system, word
