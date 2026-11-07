"""Quick agent: classification and answer() with every tool mocked (ported from v1 tests/unit/test_agent.py;
the llm tests live in test_llm.py). Added: [Doc N] on every context item, source/session in quotes."""

from unittest import mock

import pytest

from core import agent, llm, retriever
from core.tickers import Company

NVDA = Company("NVDA", "NVIDIA", 1045810)
AMD = Company("AMD", "AMD", 2488)

# ─── classify_query ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "query, expected",
    [
        ("What is NVIDIA's stock price and latest news?", ["market", "news"]),
        ("How does AMD compete with Intel?", ["rag"]),            # "pe" in "compete"
        ("What steps is Intel taking in foundry?", ["rag"]),       # "eps" in "steps"
        ("Is the market open for people who expect more?", ["rag"]),
        ("Compare NVIDIA and AMD revenue", ["rag"]),
        ("What is NVDA's P/E ratio?", ["market"]),
        ("AMD stock price and quarterly earnings", ["market", "rag"]),
        ("Any recent headlines on Intel?", ["news"]),
        ("What did NVIDIA's latest 10-Q say?", ["news", "rag"]),
        ("What risks does Intel highlight?", ["rag"]),
        ("Compare the day moves of NVIDIA, AMD and Intel", ["market", "rag"]),  # eval miss c2
    ],
)
def test_classify_query_uses_whole_words(query, expected):
    assert agent.classify_query(query) == expected


# ─── answer() ────────────────────────────────────────────────────────────────

QUOTE = {"ticker": "NVDA", "price": 200.0, "prev_close": 198.0, "open": 199.0, "high": 201.0,
         "low": 197.0, "volume": 1e6, "market_cap": 4.9e12, "pe_ratio": 50.1, "change_pct": 1.01,
         "trading_date": "2026-11-04", "source": "finnhub", "fetched_at": "2026-11-05T08:00:00+00:00"}
ARTICLE = {"title": "Nvidia rallies", "link": "https://news/1", "published": "today", "summary": None}
CHUNK = {"company": "AMD", "ticker": "AMD", "source": "AMD 10-Q", "text": "Revenue was $7B.", "score": 0.8,
         "url": "https://sec/amd", "form": "10-Q", "doc_date": "2026-11-04", "period": "2026-09-26",
         "section": "Part I, Item 2. MD&A"}


@pytest.fixture
def tools():
    with mock.patch.object(agent.tickers, "find_companies", return_value=[NVDA]) as find, \
         mock.patch.object(agent.market, "get_quote", return_value=QUOTE) as quote, \
         mock.patch.object(agent.news, "get_news", return_value=[ARTICLE]) as news, \
         mock.patch.object(agent.retriever, "search", return_value=[CHUNK]) as search, \
         mock.patch.object(agent.llm, "generate", return_value="NVDA is $200 [Doc 1]") as gen:
        yield {"find": find, "quote": quote, "news": news, "search": search, "generate": gen}


def test_answer_runs_market_and_news_and_returns_sources(tools):
    result = agent.answer("What is NVIDIA's stock price and latest news?")
    assert result["answer"] == "NVDA is $200 [Doc 1]"
    assert result["error"] is None and result["mode"] == "quick"
    assert result["used_tools"] == ["market", "news"]
    assert result["companies"] == [NVDA.to_dict()]
    assert [s["kind"] for s in result["sources"]] == ["market", "news"]
    tools["search"].assert_not_called()
    prompt = tools["generate"].call_args.args[0]
    assert "price $200.00" in prompt and "Nvidia rallies" in prompt
    assert "total" in result["timings"] and "llm" in result["timings"]


def test_every_context_item_is_numbered_and_cited_in_sources(tools):
    result = agent.answer("NVIDIA stock price, latest news and quarterly revenue")
    ctx = result["context"]
    assert "[Doc 1] NVDA quote from finnhub (session 2026-11-04" in ctx
    assert "[Doc 2] NVIDIA (NVDA) headline: Nvidia rallies" in ctx
    assert "[Doc 3] AMD | 10-Q | period 2026-09-26 | filed 2026-11-04 | Part I, Item 2. MD&A" in ctx
    assert [s["label"].split("]")[0] + "]" for s in result["sources"]] == ["[Doc 1]", "[Doc 2]", "[Doc 3]"]
    assert result["sources"][0]["url"] == "https://finnhub.io/quote/NVDA"
    assert "[Doc N] citation" in tools["generate"].call_args.args[0]


def test_answer_fetches_every_company_for_comparisons(tools):
    tools["find"].return_value = [NVDA, AMD]
    result = agent.answer("Compare NVIDIA and AMD stock price")
    tickers_quoted = sorted(c.args[0] for c in tools["quote"].call_args_list)
    assert tickers_quoted == ["AMD", "NVDA"]
    # two companies -> no single-company preference for the document search
    assert tools["search"].call_args.kwargs["ticker"] is None
    assert result["used_tools"] == ["market", "rag"]


def test_answer_prefers_the_one_company_in_document_search(tools):
    agent.answer("What are NVIDIA's revenue trends?")
    assert tools["search"].call_args.kwargs["ticker"] == "NVDA"


def test_tool_failure_is_reported_in_context_not_raised(tools):
    tools["quote"].side_effect = agent.market.MarketDataError("down")
    tools["search"].side_effect = retriever.RetrieverError("index missing")
    result = agent.answer("NVIDIA stock price and quarterly revenue")
    assert result["answer"] == "NVDA is $200 [Doc 1]"
    prompt = tools["generate"].call_args.args[0]
    assert "[market data unavailable for NVDA]" in prompt
    assert "[rag data unavailable]" in prompt


def test_empty_news_says_so(tools):
    tools["news"].return_value = []
    result = agent.answer("Latest news on NVIDIA?")
    assert "No recent headlines found for NVIDIA." in result["context"]


def test_llm_failure_returns_friendly_error(tools):
    tools["generate"].side_effect = llm.LLMUnavailableError("primary: HTTP 503 secret detail")
    result = agent.answer("How does AMD compete with Intel?")
    assert result["answer"] is None
    assert result["error"] == agent.LLM_UNAVAILABLE_MESSAGE
    assert "secret" not in str(result)


def test_market_question_without_a_company_says_so(tools):
    tools["find"].return_value = []
    agent.answer("What is the stock price?")
    tools["quote"].assert_not_called()
    assert "No company or ticker was recognised" in tools["generate"].call_args.args[0]


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_question_is_rejected(bad):
    with pytest.raises(ValueError):
        agent.answer(bad)


def test_a_slow_tool_does_not_hold_up_the_answer(tools, monkeypatch):
    import threading
    import time

    release = threading.Event()
    monkeypatch.setattr(agent, "TOOL_TIMEOUT_SECONDS", 0.5)
    tools["quote"].side_effect = lambda *a, **k: release.wait(10)  # hangs well past the deadline
    started = time.monotonic()
    result = agent.answer("NVIDIA stock price and latest news")
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 3, elapsed
    assert result["answer"] == "NVDA is $200 [Doc 1]"
    assert "[market data unavailable for NVDA]" in tools["generate"].call_args.args[0]


def test_request_id_reaches_tool_threads(tools):
    from core import log

    seen = []
    tools["quote"].side_effect = lambda t: seen.append(log.get_request_id()) or QUOTE
    log.new_request_id("req-quick")
    agent.answer("NVIDIA stock price")
    assert seen == ["req-quick"]
