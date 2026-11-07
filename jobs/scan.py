"""
The watchlist scan: price alerts (SPEC F8) with a "why did it move" line (F9).

    python cli.py scan                      # check prices, write new alerts
    python cli.py scan --dry-run            # print what would fire; write nothing, no LLM call
    python cli.py scan --replay INTC:-7.5   # demo: pretend INTC moved -7.5% today (market closed)

On AWS, aws_lambda/scan.py runs this every 15 minutes in market hours with notify=False: alerts
are only WRITTEN here and delivery is the DynamoDB Stream -> NotifyFunction, so a Telegram outage
can't slow or fail the scan. Locally (no Streams) run() then sends unsent alerts from the last
24 h to Telegram itself, marking each only after Telegram accepts it, so a failed send is retried
on the next run.

Steps: one batched quote call (watchlist + QQQ/SPY), skip the run if there is no session today
(D12), alerts.price_events() per ticker, explain new price moves once (has_event first: the
explanation costs a Gemini call and the same alert is regenerated on every scan until the day
ends), insert with the store's dedupe.

Replay (PLAN B demo; US market hours are 20:00-02:30 IST): one ticker's quote is replaced by a
synthetic move from its real previous close, dated today, and the alert is labelled "(replay)".
Its dedupe key is `replay:<key>`, so replays dedupe among themselves and never block a real alert.

Adapted from v1 jobs/scan.py (73b6fda): prices only (filing alerts and indexing cut, D13), the
no-session skip, replay, delivery through Streams.
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from core import alerts, explain, market, telegram
from core.config import setup_logging
from core.store import get_store

logger = logging.getLogger(__name__)

REPLAY_PREFIX = "replay:"
_REPLAY_SPEC = re.compile(r"^\s*([A-Za-z.\-]{1,10})\s*:\s*([+-]?\d+(?:\.\d+)?)\s*%?\s*$")


def parse_replay(spec: str) -> tuple[str, float]:
    """'INTC:-7.5' -> ('INTC', -7.5)."""
    m = _REPLAY_SPEC.match(spec or "")
    if not m or not 0 < abs(float(m.group(2))) <= 50:
        raise ValueError("Replay must look like TICKER:PCT, e.g. INTC:-7.5 (|PCT| ≤ 50)")
    return market.normalise_ticker(m.group(1)), float(m.group(2))


def replay_quote(real: dict | None, ticker: str, pct: float, today: str) -> dict:
    """A synthetic quote: `pct` from the real previous close (or last price), dated today."""
    base = (real or {}).get("prev_close") or (real or {}).get("price")
    if not base:
        raise market.MarketDataError(f"No real quote for {ticker} to replay from")
    price = round(base * (1 + pct / 100), 2)
    return {"ticker": ticker, "price": price, "prev_close": base, "change": round(price - base, 2),
            "change_pct": round(pct, 2), "trading_date": today,
            "source": f"replay of {(real or {}).get('source') or 'quote'}", "replay": True}


def _as_replay(event: dict, fail_delivery: bool = False) -> dict:
    out = {**event, "title": f"{event['title']} (replay)", "dedupe_key": REPLAY_PREFIX + event["dedupe_key"],
           "body": "Demo replay: this move did not happen. " + event["body"]}
    if fail_delivery:  # demo of the DLQ path: NotifyFunction reports this alert as a failed send
        out["dedupe_key"] += ":fail"
        out["fail_delivery"] = True
    return out


def scan_prices(store, watchlist: list[dict], dry_run: bool, replay: tuple[str, float] | None = None,
                now: datetime | None = None, fail_delivery: bool = False) -> tuple[list[dict], str | None]:
    """Returns (new events, skip reason or None)."""
    today = alerts.new_york_today(now)
    symbols = [w["ticker"] for w in watchlist]
    if replay and replay[0] not in symbols:
        watchlist = watchlist + [{"ticker": replay[0], "name": replay[0]}]
        symbols.append(replay[0])
    try:
        # Benchmarks ride along in the same batched download (for "moved with the market").
        quotes = dict(market.get_quotes(symbols + list(explain.BENCHMARKS)))  # replay edits a copy
    except market.MarketDataError as e:
        logger.error("Price scan skipped: %s", e)
        return [], f"market data unavailable: {e}"
    if replay:
        quotes[replay[0]] = replay_quote(quotes.get(replay[0]), replay[0], replay[1], today)
    elif not any(alerts.is_current(q, today) for q in quotes.values()):
        # No ticker (benchmarks included) traded today: a holiday, a weekend or stale data (D12).
        logger.info("No trading data for %s (holiday, weekend or stale quotes); price rules skipped", today,
                    extra={"no_session": today})
        return [], f"no trading data for {today} (D12)"

    bench = {sym: quotes.get(sym) for sym in explain.BENCHMARKS}
    created = []
    for item in watchlist:
        symbol = market.normalise_ticker(item["ticker"])
        quote = quotes.get(symbol)
        if replay and symbol != replay[0]:
            continue  # a replay touches only its ticker
        if quote and quote.get("error"):
            logger.warning("%s: %s", symbol, quote["error"])
        elif quote and not alerts.is_current(quote, today):
            logger.info("%s: quote is for %s, not %s; skipped (D12)", symbol, quote.get("trading_date"), today)
        for event in alerts.price_events(item, quote, today):
            if quote.get("replay"):
                event = _as_replay(event, fail_delivery)
            if dry_run:
                created.append(event)
                continue
            if store.has_event(event["dedupe_key"]):
                logger.info("Already alerted: %s", event["dedupe_key"])
                continue
            if event["kind"] == "price_move":
                try:
                    event["body"] = f"{event['body']}\nWhy: {explain.explain_move(item, quote, bench)}"
                except Exception as e:  # never lose the alert over its explanation
                    logger.warning("Move explanation failed for %s: %s", symbol, type(e).__name__)
            row = store.insert_event(event)
            if row:
                created.append(row)
            else:
                logger.info("Already alerted: %s", event["dedupe_key"])
    return created, None


def notify_pending(store) -> int:
    """Local delivery (no Streams): unsent alerts from the last 24 h -> Telegram."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    pending = store.unnotified_events(since)
    if pending and not telegram.is_configured():
        logger.info("%d alert(s) waiting; Telegram is not configured", len(pending))
        return 0
    sent = 0
    for event in pending:
        if telegram.send(telegram.format_event(event)):
            store.mark_notified(event["id"])
            sent += 1
    return sent


def run(dry_run: bool = False, replay: str | None = None, now: datetime | None = None,
        notify: bool = True, fail_delivery: bool = False) -> dict:
    """notify=False when something else delivers alerts (on AWS: DynamoDB Streams -> NotifyFunction)."""
    if fail_delivery and not replay:
        raise ValueError("fail_delivery is only for a replay (demo of the DLQ path)")
    started = time.monotonic()
    store = get_store()
    watchlist = store.list_watchlist()
    events, skipped = scan_prices(store, watchlist, dry_run, parse_replay(replay) if replay else None, now,
                                  fail_delivery)
    summary = {"store": store.name, "tickers": [w["ticker"] for w in watchlist], "dry_run": dry_run,
               "replay": replay, "price_events": [e["title"] for e in events], "skipped": skipped,
               "notified": notify_pending(store) if notify and not dry_run else 0}
    summary["seconds"] = round(time.monotonic() - started, 1)
    logger.info("Scan: %d price event(s)%s", len(events), f", skipped: {skipped}" if skipped else "",
                extra={"price_events": len(events), "skipped": skipped})
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scan the watchlist for price moves.")
    parser.add_argument("--dry-run", action="store_true", help="print hits; write nothing")
    parser.add_argument("--replay", metavar="TICKER:PCT", help="demo: replay a move, e.g. INTC:-7.5")
    args = parser.parse_args(argv)
    setup_logging()
    try:
        summary = run(dry_run=args.dry_run, replay=args.replay)
    except ValueError as e:
        print(e)
        return 2
    except Exception as e:
        logger.exception("Scan failed: %s", type(e).__name__)
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
