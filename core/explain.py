"""
"Why did it move" (SPEC F9). A wrong explanation is worse than none, so the steps go from
cheapest-and-certain to least certain, and the default is honesty:

1. Market check (code, no LLM): if QQQ or SPY moved at least 1.5% the same way and
   the stock's move is no more than 1.5x that, it "moved with the market".
2. Headline check (code): only headlines published between the previous session's
   close and this session's close (4 pm New York) that name the company or ticker are
   candidates. After-hours news explains the NEXT session, not this one (a live test
   caught an after-close earnings headline being credited for the day's move).
   None -> "no clear catalyst found".
3. LLM pick (JSON): one candidate id or none. The id must exist in the list, and the
   alert quotes the headline's own title, never the model's paraphrase.

Known gap (D12): no holiday calendar, so the day after a market holiday the window starts at the
holiday's 4 pm instead of the previous session's close.

Ported from v1 core/explain.py (73b6fda); quotes carry `trading_date` (was `market_date`).
"""

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from google.genai import types

from core import llm, news

logger = logging.getLogger(__name__)

BENCHMARKS = {"QQQ": "Nasdaq-100", "SPY": "S&P 500"}
MARKET_MIN_PCT = 1.5        # the market itself must have moved this much
MARKET_RATIO = 1.5          # ...and the stock no more than 1.5x the market
NEW_YORK = ZoneInfo("America/New_York")
CLOSE = time(16, 0)
MAX_CANDIDATES = 8
NO_CATALYST = "No clear catalyst found in recent headlines."

PICK_SCHEMA = types.Schema(type="OBJECT", properties={
    "headline_id": types.Schema(type="STRING", description="id of the ONE headline that explains the move, or 'none'"),
}, required=["headline_id"])
PICK_SYSTEM = """A stock moved sharply today. From the numbered headlines, pick the ONE that
most plausibly explains a move in that direction (earnings, guidance, a deal, a
downgrade, regulation, ...). If none clearly explains it, answer "none". Never pick a
headline just because it mentions the company."""


def market_context(pct: float, bench: dict) -> tuple[str | None, str]:
    """Returns (moved-with-market sentence or None, short benchmark note)."""
    moves = [(sym, q["change_pct"]) for sym, q in bench.items()
             if q and not q.get("error") and q.get("change_pct") is not None]
    if not moves:
        return None, ""
    note = ", ".join(f"{sym} {chg:+.1f}%" for sym, chg in moves)
    same_way = [(sym, chg) for sym, chg in moves if chg * pct > 0 and abs(chg) >= MARKET_MIN_PCT]
    if same_way:
        sym, chg = max(same_way, key=lambda m: abs(m[1]))
        if abs(pct) <= MARKET_RATIO * abs(chg):
            return f"Moved with the market ({BENCHMARKS.get(sym, sym)} {sym} {chg:+.1f}% the same session).", note
    return None, note


def _published(article: dict) -> datetime | None:
    try:
        dt = parsedate_to_datetime(article.get("published") or "")
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def session_window(session_date: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """(previous session's close, this session's close or now if earlier), in UTC."""
    now = now or datetime.now(timezone.utc)
    day = date.fromisoformat(session_date)
    prev = day - timedelta(days=3 if day.weekday() == 0 else 1)  # Monday -> Friday's close
    start = datetime.combine(prev, CLOSE, NEW_YORK).astimezone(timezone.utc)
    end = datetime.combine(day, CLOSE, NEW_YORK).astimezone(timezone.utc)
    return start, min(end, now)


def candidate_headlines(ticker: str, company: str | None, session_date: str,
                        now: datetime | None = None) -> list[dict]:
    """Headlines that name the company and were published during the session's window."""
    start, end = session_window(session_date, now)
    keywords = news.name_keywords(ticker, company)
    out = []
    for a in news.get_news(ticker, company, 20):
        when = _published(a)
        if when is None or not (start <= when <= end):
            continue
        if news._mentions(a["title"], keywords):
            out.append({**a, "when": when})
    return out[:MAX_CANDIDATES]


def explain_move(item: dict, quote: dict, bench: dict, now: datetime | None = None) -> str:
    """One sentence for a price_move alert."""
    pct = quote.get("change_pct") or 0.0
    with_market, note = market_context(pct, bench)
    if with_market:
        return with_market
    suffix = f" (market: {note})" if note else ""
    try:
        session = quote.get("trading_date") or (now or datetime.now(timezone.utc)).date().isoformat()
        heads = candidate_headlines(item["ticker"], item.get("name"), session, now)
    except news.NewsError:
        return f"Headlines unavailable; no catalyst checked.{suffix}"
    if not heads:
        return NO_CATALYST + suffix

    listing = "\n".join(f"h{i}: {h['title']} ({h['when']:%b %d %H:%M} UTC)" for i, h in enumerate(heads))
    prompt = (f"{item['ticker']} ({item.get('name') or item['ticker']}) moved {pct:+.1f}% "
              f"in the {quote.get('trading_date', 'latest')} session. Market: {note or 'n/a'}.\n\n"
              f"HEADLINES:\n{listing}")
    config = types.GenerateContentConfig(system_instruction=PICK_SYSTEM, temperature=0, max_output_tokens=60,
                                         response_mime_type="application/json", response_schema=PICK_SCHEMA)
    try:
        choice = json.loads(llm.call(prompt, config, budget_seconds=25).text).get("headline_id", "none")
    except (llm.LLMUnavailableError, ValueError) as e:
        logger.warning("Move explainer LLM unavailable: %s", e)
        latest = max(heads, key=lambda h: h["when"])
        return f"Latest headline (not verified as the cause): “{latest['title']}”.{suffix}"
    ids = {f"h{i}": h for i, h in enumerate(heads)}
    picked = ids.get(str(choice).strip())
    if picked is None:  # "none", or an id the model invented
        return NO_CATALYST + suffix
    return f"Likely catalyst: “{picked['title']}” ({picked['when']:%b %d %H:%M} UTC){suffix}."
