"""
Price alert rules (SPEC F8). Pure functions: a watchlist row + a quote in, event rows out.

Rules per ticker (columns on the watchlist row):
- pct_move:     |day % change| >= pct_move   (default ±3%)
- price_above:  price >= price_above         (optional)
- price_below:  price <= price_below         (optional)

Each event carries a dedupe_key built from ticker + rule + trading date, and the store keeps
dedupe_key unique (SQLite UNIQUE / DynamoDB transaction), so every rule fires at most once per
trading day no matter how often the scan runs.

No session, no alert (D12): if the quote's `trading_date` isn't today's New York date (a market
holiday, a weekend, or a stale quote), its rules are skipped, never guessed from old numbers.

Adapted from v1 core/alerts.py (73b6fda): `trading_date` (was `market_date`), the no-session skip.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.store import DEFAULT_PCT_MOVE

NEW_YORK = ZoneInfo("America/New_York")


def new_york_today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(NEW_YORK).date().isoformat()


def _session_label(day: str) -> str:
    """'2026-11-02' -> 'Nov 02' (the alert may be read the next morning, so no 'today')."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%b %d")
    except ValueError:
        return day


def _quote_url(ticker: str) -> str:
    return f"https://finance.yahoo.com/quote/{ticker}"


def is_current(quote: dict | None, today: str) -> bool:
    """True if the quote has a price for today's New York session (D12)."""
    return bool(quote and not quote.get("error") and quote.get("price") is not None
                and quote.get("trading_date") == today)


def price_events(item: dict, quote: dict | None, today: str) -> list[dict]:
    """Events this quote triggers for this watchlist row. Empty if none, no data, or the quote
    isn't from today's session (`today` = New York date, see new_york_today())."""
    if not is_current(quote, today):
        return []
    ticker = item["ticker"]
    name = item.get("name") or ticker
    price = quote["price"]
    day = quote["trading_date"]
    source = quote.get("source") or "unknown source"
    events = []

    pct = quote.get("change_pct")
    threshold = item.get("pct_move")
    threshold = DEFAULT_PCT_MOVE if threshold is None else float(threshold)
    if pct is not None and threshold > 0 and abs(pct) >= threshold:
        direction = "up" if pct > 0 else "down"
        events.append({
            "ticker": ticker,
            "kind": "price_move",
            "title": f"{ticker} {pct:+.1f}% on {_session_label(day)}",
            "body": (f"{name} is at ${price:,.2f}, {quote.get('change') or 0:+,.2f} vs the previous "
                     f"close of ${quote.get('prev_close') or 0:,.2f} (alert threshold ±{threshold:g}%; "
                     f"quote: {source})."),
            "url": _quote_url(ticker),
            "dedupe_key": f"{ticker}:move_{direction}:{day}",
        })

    above = item.get("price_above")
    if above is not None and price >= float(above):
        events.append({
            "ticker": ticker,
            "kind": "price_above",
            "title": f"{ticker} is above ${float(above):,.2f}",
            "body": f"{name} is at ${price:,.2f}, above your target of ${float(above):,.2f} (quote: {source}).",
            "url": _quote_url(ticker),
            "dedupe_key": f"{ticker}:above:{float(above):g}:{day}",
        })

    below = item.get("price_below")
    if below is not None and price <= float(below):
        events.append({
            "ticker": ticker,
            "kind": "price_below",
            "title": f"{ticker} is below ${float(below):,.2f}",
            "body": f"{name} is at ${price:,.2f}, below your floor of ${float(below):,.2f} (quote: {source}).",
            "url": _quote_url(ticker),
            "dedupe_key": f"{ticker}:below:{float(below):g}:{day}",
        })
    return events
