"""
Quick mode: question in, sourced and timestamped answer out, in one Gemini call (SPEC F5, D5).

1. classify_query() picks tools with whole-word keyword rules (market / news / rag).
2. tickers.find_companies() finds up to 3 companies in the question (any US ticker).
3. The chosen tools run IN PARALLEL (thread pool, one shared deadline): one quote and one news
   call per company, plus one document search.
4. Every piece of context is numbered [Doc N] (quotes, headlines and filing excerpts alike) and
   Gemini writes the answer citing [Doc N] for every number (core.llm fallback chain).

Ported from v1 core/agent.py (73b6fda). Changes: every context block gets a [Doc N] label (v1
labelled only filing excerpts, so quote and news numbers could not be cited: SPEC F5 asks for a
citation on every number); quotes show their source and trading date; filing excerpts show form,
period and section; the request ID follows the tool threads.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from core import ingest, llm, market, news, retriever, tickers
from core.log import run_in_context

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_SECONDS = 25
NEWS_PER_COMPANY = 5

LLM_UNAVAILABLE_MESSAGE = "The AI model is busy or unavailable right now. Please try again in a minute."

# ─── 1. Query classification ────────────────────────────────────────────────
# Whole-word patterns, so "pe" doesn't match "compete" and "eps" doesn't match "steps".
# "moves" etc. added after the 2026-11-03 eval: "Compare the day moves of ..." got no market data.
MARKET_PATTERN = re.compile(
    r"\b(?:prices?|stocks?|shares?|pe|p/e|market ?cap|volume|eps|valuation|trading|quote"
    r"|moves?|moved|movement|day change)\b"
)
NEWS_PATTERN = re.compile(r"\b(?:news|latest|recent|headlines?|articles?)\b")
# Stems are fine here: "compared", "earnings", "quarterly" should all trigger RAG.
RAG_PATTERN = re.compile(
    r"\b(?:revenue|earning|profit|quarter|annual|forecast|guidance|compar|analy|filing|10-?[kq]|8-?k|risk|margin)\w*"
)


def classify_query(query: str) -> list[str]:
    """Tools to use: any of 'market', 'news', 'rag' (rag is the default)."""
    q = query.lower()
    tools = []
    if MARKET_PATTERN.search(q):
        tools.append("market")
    if NEWS_PATTERN.search(q):
        tools.append("news")
    if not tools or RAG_PATTERN.search(q):
        tools.append("rag")
    return tools


# ─── 2. Tool formatting ─────────────────────────────────────────────────────

def _fmt_money(value) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1e9:
        return f"${value / 1e9:,.1f}B"
    return f"${value:,.2f}"


def format_quote(q: dict) -> str:
    change = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else "N/A"
    pe = f"{q['pe_ratio']:.1f}" if q.get("pe_ratio") else "N/A"
    volume = f"{q['volume']:,.0f}" if q.get("volume") else "N/A"
    return (f"{q['ticker']} quote from {q.get('source', 'yfinance')} (session {q.get('trading_date') or 'n/a'}, "
            f"fetched {q['fetched_at']}): price {_fmt_money(q.get('price'))}, "
            f"previous close {_fmt_money(q.get('prev_close'))}, day change {change}, "
            f"open {_fmt_money(q.get('open'))}, high {_fmt_money(q.get('high'))}, low {_fmt_money(q.get('low'))}, "
            f"volume {volume}, market cap {_fmt_money(q.get('market_cap'))}, P/E {pe}")


def format_article(company: tickers.Company, a: dict) -> str:
    return f"{company.name} ({company.ticker}) headline: {a['title']} ({a.get('published', '')})"


def chunk_label(c: dict) -> str:
    parts = [c.get("ticker") or c.get("company"), c.get("form") or c.get("source") or "document"]
    if c.get("period") or c.get("event_date"):
        parts.append(ingest.describe_period(c.get("form") or "", c.get("period") or "", c.get("event_date") or ""))
    if c.get("doc_date"):
        parts.append(f"filed {c['doc_date']}")
    if c.get("section"):
        parts.append(c["section"])
    return " | ".join(str(p) for p in parts if p)


# ─── 3. Prompt ──────────────────────────────────────────────────────────────

def build_prompt(query: str, context: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""You are an investment research assistant. Today is {today} (UTC).

QUESTION:
{query}

CONTEXT (live market data, recent headlines and excerpts from SEC filings; each item is labelled [Doc N]):
{context}

Instructions:
- Answer strictly from the context. If it is not enough, say what is missing.
- Use concrete numbers and say how current they are (quote session, filing period and date).
- Cite every number and every claim with its label, e.g. "Revenue was $46.7 billion [Doc 3]".
  A line that contains a number must carry a [Doc N] citation.
- Never use numbers from memory; only numbers that appear in the context.
- Be concise and structured (short sections or bullets). No investment advice.
"""


# ─── 4. Main entry point ────────────────────────────────────────────────────

def _timed(fn, *args, **kwargs):
    started = time.monotonic()
    try:
        return fn(*args, **kwargs), None, time.monotonic() - started
    except Exception as e:  # each tool fails on its own; the answer goes on without it
        return None, e, time.monotonic() - started


def _rag_search(query: str, companies: list[tickers.Company]) -> list[dict]:
    """Prefers that company's documents when the question is about exactly one company."""
    return retriever.search(query, ticker=companies[0].ticker if len(companies) == 1 else None)


def answer(query: str) -> dict:
    """
    Returns {answer, error, used_tools, companies, sources, as_of, timings, mode, context}.
    `error` is a user-safe message when the LLM failed (answer is then None).
    Raises ValueError for an empty question.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("Missing question")
    started = time.monotonic()

    tools = classify_query(query)
    companies = tickers.find_companies(query)
    logger.info("Agent: tools=%s companies=%s", tools, [c.ticker for c in companies])

    # Plan every tool call, then run them all at once.
    jobs = {}
    if "market" in tools:
        for c in companies:
            jobs[("market", c.ticker)] = (market.get_quote, (c.ticker,), {})
    if "news" in tools:
        for c in companies:
            jobs[("news", c.ticker)] = (news.get_news, (c.ticker, c.name, NEWS_PER_COMPANY), {})
    if "rag" in tools:
        jobs[("rag", "")] = (_rag_search, (query, companies), {})

    results = {}
    if jobs:
        # One shared deadline for all tools, and no waiting on stragglers: a `with` block
        # would join every thread on exit, so one slow tool could hold up the answer.
        pool = ThreadPoolExecutor(max_workers=min(8, len(jobs)))
        tools_deadline = time.monotonic() + TOOL_TIMEOUT_SECONDS
        try:
            futures = {key: pool.submit(run_in_context(_timed), fn, *args, **kw)
                       for key, (fn, args, kw) in jobs.items()}
            for key, future in futures.items():
                try:
                    results[key] = future.result(timeout=max(0.0, tools_deadline - time.monotonic()))
                except Exception as e:
                    results[key] = (None, e, TOOL_TIMEOUT_SECONDS)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    sections, sources, used_tools, timings = [], [], [], {}
    by_ticker = {c.ticker: c for c in companies}
    n = 0

    def doc(text: str, source: dict) -> str:
        nonlocal n
        n += 1
        sources.append({**source, "label": f"[Doc {n}] {source['label']}"})
        return f"[Doc {n}] {text}"

    for (tool, ticker), (value, error, seconds) in results.items():
        timings[f"{tool}:{ticker}" if ticker else tool] = round(seconds, 2)
        if tool not in used_tools:
            used_tools.append(tool)
        if error is not None:
            logger.warning("Tool %s %s failed: %s", tool, ticker, type(error).__name__)
            sections.append(f"[{tool} data unavailable{' for ' + ticker if ticker else ''}]")
            continue
        if tool == "market":
            url = (f"https://finnhub.io/quote/{ticker}" if value.get("source") == "finnhub"
                   else f"https://finance.yahoo.com/quote/{ticker}")
            sections.append("MARKET DATA\n" + doc(format_quote(value), {
                "kind": "market", "label": f"{value.get('source', 'yfinance')} quote: {ticker} "
                                           f"(session {value.get('trading_date') or 'n/a'})", "url": url}))
        elif tool == "news":
            company = by_ticker[ticker]
            if not value:
                sections.append(f"NEWS\nNo recent headlines found for {company.name}.")
                continue
            lines = [doc(format_article(company, a), {"kind": "news", "label": a["title"], "url": a.get("link")})
                     for a in value]
            sections.append("NEWS\n" + "\n".join(lines))
        elif tool == "rag":
            if not value:
                sections.append("FILING EXCERPTS\nNo matching documents.")
                continue
            blocks = [doc(f"{chunk_label(c)}\n{c['text'].strip()[:900]}",
                          {"kind": "document", "label": chunk_label(c), "url": c.get("url")}) for c in value]
            sections.append("FILING EXCERPTS\n" + "\n\n".join(blocks))

    if "market" in tools and not companies:
        sections.append("[No company or ticker was recognised in the question, so no live quote was fetched.]")

    context = "\n\n".join(sections) or "No additional context available."
    result = {
        "answer": None, "error": None, "used_tools": used_tools,
        "companies": [c.to_dict() for c in companies], "sources": sources,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"), "timings": timings,
        "mode": "quick",
        "context": context,  # the evidence the answer was written from (used by the eval)
    }

    llm_started = time.monotonic()
    try:
        result["answer"] = llm.generate(build_prompt(query, context))
    except llm.LLMUnavailableError as e:
        logger.error("LLM unavailable: %s", e)
        result["error"] = LLM_UNAVAILABLE_MESSAGE
    timings["llm"] = round(time.monotonic() - llm_started, 2)
    timings["total"] = round(time.monotonic() - started, 2)
    logger.info("Agent latency: total=%.1fs llm=%.1fs", timings["total"], timings["llm"],
                extra={"mode": "quick", "timings": timings})
    return result
