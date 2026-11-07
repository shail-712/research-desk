"""
Tools the agents can call (Gemini function declarations) and their implementations.

Each tool wraps an existing core/ function, writes its results to the evidence
board, and returns a compact dict for the model that includes the evidence IDs.
Failures come back as {"error": "..."} so the model can note a gap instead of crashing.

Adapted from v1 core/agents/tools.py (73b6fda): quotes carry their source and trading date;
filing passages carry form, period, filing date and section; latest_filing lists the latest 10-K,
10-Q and earnings 8-K (core.ingest.latest_filings) instead of v1's watcher module (cut, D13).
"""

import logging

from google.genai import types

from core import ingest, market, news, retriever, sec, tickers
from core.agents.evidence import EvidenceBoard
from core.store import get_store

logger = logging.getLogger(__name__)

S = types.Schema
STRING, INTEGER, ARRAY, OBJECT = "STRING", "INTEGER", "ARRAY", "OBJECT"

DECLARATIONS = {
    "get_quote": types.FunctionDeclaration(
        name="get_quote",
        description="Live quote for ONE US stock: price, day change %, open/high/low, volume, market cap, P/E.",
        parameters=S(type=OBJECT, properties={"ticker": S(type=STRING, description="e.g. NVDA")},
                     required=["ticker"])),
    "get_quotes": types.FunctionDeclaration(
        name="get_quotes",
        description="Price and day change % for SEVERAL US stocks at once (use for comparisons).",
        parameters=S(type=OBJECT, properties={"tickers": S(type=ARRAY, items=S(type=STRING))},
                     required=["tickers"])),
    "get_news": types.FunctionDeclaration(
        name="get_news",
        description="Latest news headlines (with publish times) for one US stock.",
        parameters=S(type=OBJECT, properties={
            "ticker": S(type=STRING), "count": S(type=INTEGER, description="1-8, default 5")},
            required=["ticker"])),
    "recent_alerts": types.FunctionDeclaration(
        name="recent_alerts",
        description="Recent price-move alerts and new SEC filings recorded by the watcher.",
        parameters=S(type=OBJECT, properties={"ticker": S(type=STRING, description="optional filter")})),
    "search_filings": types.FunctionDeclaration(
        name="search_filings",
        description=("Semantic search over company SEC filings (10-K, 10-Q, 8-K text): revenue, "
                     "segments, guidance, risks, strategy. Returns the best-matching passages."),
        parameters=S(type=OBJECT, properties={
            "query": S(type=STRING, description="a focused search query"),
            "ticker": S(type=STRING, description="optional: prefer this company's filings")},
            required=["query"])),
    "latest_filing": types.FunctionDeclaration(
        name="latest_filing",
        description=("The company's latest filed 10-K, 10-Q and earnings 8-K at the SEC: form, fiscal period "
                     "end (8-K: event date), filing date and link. Use it to state which quarter/year the "
                     "filings cover. It has no financial figures: use search_filings for those."),
        parameters=S(type=OBJECT, properties={"ticker": S(type=STRING)}, required=["ticker"])),
}


def _company(ticker: str) -> tickers.Company:
    symbol = market.normalise_ticker(ticker)
    return tickers.lookup_ticker(symbol) or tickers.Company(symbol, symbol, None)


def _money(v) -> str:
    if v is None:
        return "N/A"
    return f"${v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"${v:,.2f}"


def _quote_source(q: dict) -> tuple[str, str]:
    """(label, url) for a quote, naming the provider that actually served it."""
    symbol = q["ticker"]
    if q.get("source") == "finnhub":
        return f"Finnhub quote: {symbol}", f"https://finnhub.io/quote/{symbol}"
    return f"Yahoo Finance quote: {symbol}", f"https://finance.yahoo.com/quote/{symbol}"


def _get_quote(board: EvidenceBoard, agent: str, ticker: str) -> dict:
    q = market.get_quote(ticker)
    pct = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else "N/A"
    pe = f"{q['pe_ratio']:.1f}" if q.get("pe_ratio") else "N/A"
    summary = (f"{q['ticker']} price {_money(q['price'])}, day change {pct} vs previous close "
               f"{_money(q.get('prev_close'))}; open {_money(q.get('open'))}, high {_money(q.get('high'))}, "
               f"low {_money(q.get('low'))}, volume {q.get('volume') or 0:,.0f}, "
               f"market cap {_money(q.get('market_cap'))}, P/E {pe} "
               f"(session {q.get('trading_date') or 'n/a'}, source {q.get('source', 'yfinance')})")
    ev = board.add(agent, "get_quote", summary, *_quote_source(q), "market")
    return {"evidence": [{"id": ev.id, "text": summary}]}


def _get_quotes(board: EvidenceBoard, agent: str, tickers_: list) -> dict:
    quotes = market.get_quotes([str(t) for t in (tickers_ or [])][:6])
    out = []
    for symbol, q in quotes.items():
        if q.get("error"):
            out.append({"ticker": symbol, "error": q["error"]})
            continue
        pct = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else "N/A"
        summary = (f"{symbol} price {_money(q['price'])}, day change {pct} vs previous "
                   f"close {_money(q.get('prev_close'))} (session {q.get('trading_date') or 'n/a'}, "
                   f"source {q.get('source', 'yfinance')})")
        ev = board.add(agent, "get_quotes", summary, *_quote_source(q), "market")
        out.append({"id": ev.id, "text": summary})
    return {"evidence": out}


def _get_news(board: EvidenceBoard, agent: str, ticker: str, count=5) -> dict:
    company = _company(ticker)
    articles = news.get_news(company.ticker, company.name, min(news.safe_count(count), 8))
    out = []
    for a in articles:
        summary = f"{company.ticker} headline ({a.get('published', '')}): {a['title']}"
        ev = board.add(agent, "get_news", summary, a["title"], a.get("link"), "news")
        out.append({"id": ev.id, "text": summary})
    return {"evidence": out} if out else {"evidence": [], "note": "no recent headlines"}


def _recent_alerts(board: EvidenceBoard, agent: str, ticker: str | None = None) -> dict:
    events = get_store().recent_events(30)
    if ticker:
        events = [e for e in events if e["ticker"] == market.normalise_ticker(ticker)]
    out = []
    for e in events[:8]:
        summary = f"{e['ticker']} alert at {e['created_at']}: {e['title']}. {e.get('body') or ''}".strip()
        ev = board.add(agent, "recent_alerts", summary, e["title"], e.get("url"), "alert")
        out.append({"id": ev.id, "text": summary})
    return {"evidence": out} if out else {"evidence": [], "note": "no recent alerts"}


def _search_filings(board: EvidenceBoard, agent: str, query: str, ticker: str | None = None) -> dict:
    chunks = retriever.search(query, top_k=4, ticker=market.normalise_ticker(ticker) if ticker else None)
    out = []
    for c in chunks:
        label = f"{c.get('ticker') or c['company']}: {c.get('form') or c.get('source')}"
        if c.get("period") or c.get("event_date"):
            label += ", " + ingest.describe_period(c.get("form") or "", c.get("period") or "", c.get("event_date") or "")
        if c.get("doc_date"):
            label += f", filed {c['doc_date']}"
        if c.get("section"):
            label += f", {c['section']}"
        summary = f"[{label}] {c['text'].strip()[:1200]}"
        ev = board.add(agent, "search_filings", summary, label, c.get("url"), "document")
        out.append({"id": ev.id, "text": summary})
    return {"evidence": out} if out else {"evidence": [], "note": "no matching passages"}


def _latest_filing(board: EvidenceBoard, agent: str, ticker: str) -> dict:
    company = _company(ticker)
    if not company.cik:
        return {"error": f"No SEC company id known for {company.ticker}"}
    if not sec.is_configured():
        return {"error": "SEC access is not configured on this deployment"}
    latest = ingest.latest_filings(company.ticker, company.cik, company.name)
    if not latest:
        return {"evidence": [], "note": "no 10-K, 10-Q or earnings 8-K found"}
    out = []
    for f in latest:
        if f.form == "8-K":
            # reportDate of an 8-K is the event date, never a fiscal period (review A, M1).
            covers = (f"; its results are for the period ending {f.period} (per the matching 10-Q/10-K)"
                      if f.period else "; the fiscal period is stated in the release itself")
            summary = (f"{company.ticker} latest earnings release 8-K: event date {f.event_date or f.filed}, "
                       f"filed {f.filed}{covers} (accession {f.accession})")
        else:
            summary = (f"{company.ticker} latest {f.form}: fiscal period ended {f.period}, filed {f.filed} "
                       f"(accession {f.accession})")
        ev = board.add(agent, "latest_filing", summary, f"SEC {company.ticker} {f.form} {f.filed}", f.url, "document")
        out.append({"id": ev.id, "text": summary})
    return {"evidence": out}


_IMPL = {
    "get_quote": lambda b, a, args: _get_quote(b, a, args.get("ticker", "")),
    "get_quotes": lambda b, a, args: _get_quotes(b, a, args.get("tickers") or []),
    "get_news": lambda b, a, args: _get_news(b, a, args.get("ticker", ""), args.get("count", 5)),
    "recent_alerts": lambda b, a, args: _recent_alerts(b, a, args.get("ticker")),
    "search_filings": lambda b, a, args: _search_filings(b, a, args.get("query", ""), args.get("ticker")),
    "latest_filing": lambda b, a, args: _latest_filing(b, a, args.get("ticker", "")),
}


def declarations(names: list[str]) -> types.Tool:
    return types.Tool(function_declarations=[DECLARATIONS[n] for n in names])


def run_tool(name: str, args: dict, board: EvidenceBoard, agent: str, deadline: float | None = None) -> dict:
    """Executes one tool call. Never raises: errors become {"error": "..."} for the model.
    `deadline` (time.monotonic) lets slow providers skip optional calls (market.deadline)."""
    if name not in _IMPL:
        return {"error": f"Unknown tool {name}"}
    try:
        with market.deadline(deadline):
            return _IMPL[name](board, agent, dict(args or {}))
    except (market.MarketDataError, news.NewsError, retriever.RetrieverError, sec.SecError) as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": f"Bad arguments: {e}"}
    except Exception as e:
        logger.warning("Tool %s failed: %s", name, type(e).__name__)
        return {"error": f"{name} is unavailable right now"}
