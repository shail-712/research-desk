"""The price scan, end to end on the local SQLite store and on moto DynamoDB (market data and the
move explainer mocked). Adapted from v1 tests/unit/test_scan.py (73b6fda): prices only, the
no-session rule (D12), replay."""

from datetime import datetime, timezone
from unittest import mock

import pytest

from core import explain
from core import store as store_mod
from jobs import scan

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)   # Tue 13:00 New York, in session
TODAY = "2026-11-03"


def q(ticker, pct, price=100.0, day=TODAY, **kw):
    prev = round(price / (1 + pct / 100), 2)
    return {"ticker": ticker, "price": price, "prev_close": prev, "change": round(price - prev, 2),
            "change_pct": pct, "trading_date": day, "source": "yfinance", **kw}


QUOTES = {"NVDA": q("NVDA", 5.0, 210.0), "AMD": q("AMD", 1.01), "INTC": {"ticker": "INTC", "error": "No price data"},
          "QQQ": q("QQQ", -0.4), "SPY": q("SPY", -0.2)}


@pytest.fixture
def quotes():
    with mock.patch.object(scan.market, "get_quotes", return_value=dict(QUOTES)) as gq:
        yield gq


@pytest.fixture
def why():
    with mock.patch.object(explain, "explain_move", return_value="Likely catalyst: “X”.") as ex:
        yield ex


def test_dry_run_reports_hits_and_writes_nothing(quotes, why, local_store):
    summary = scan.run(dry_run=True, now=NOW)
    assert summary["price_events"] == ["NVDA +5.0% on Nov 03"] and summary["skipped"] is None
    assert local_store.recent_events() == []
    why.assert_not_called()  # no Gemini call on a dry run
    assert quotes.call_args.args[0] == ["NVDA", "AMD", "INTC", "QQQ", "SPY"]  # one batched call


def test_duplicate_scan_creates_one_alert_and_explains_it_once(quotes, why, local_store):
    first = scan.run(now=NOW)
    second = scan.run(now=NOW)
    assert first["price_events"] == ["NVDA +5.0% on Nov 03"] and second["price_events"] == []
    events = local_store.recent_events()
    assert len(events) == 1 and events[0]["body"].endswith("\nWhy: Likely catalyst: “X”.")
    why.assert_called_once()  # the second scan sees the alert recorded and skips the LLM


def test_duplicate_scan_creates_one_alert_on_dynamodb(quotes, why, dynamo_store, monkeypatch):
    dynamo_store.seed_watchlist(store_mod.DEFAULT_WATCHLIST)
    monkeypatch.setattr(store_mod, "_store", dynamo_store)
    assert scan.run(now=NOW)["price_events"] == ["NVDA +5.0% on Nov 03"]
    assert scan.run(now=NOW)["price_events"] == []
    assert len(dynamo_store.recent_events()) == 1


@pytest.mark.parametrize("day", ["2026-11-02", None])
def test_stale_or_holiday_quotes_create_no_alerts(day, why, local_store):
    """D12: yesterday's numbers (a holiday, or stale data) never produce an alert today."""
    stale = {t: {**v, "trading_date": day} if "error" not in v else v for t, v in QUOTES.items()}
    with mock.patch.object(scan.market, "get_quotes", return_value=stale):
        summary = scan.run(now=NOW)
    assert summary["price_events"] == [] and summary["skipped"] == "no trading data for 2026-11-03 (D12)"
    assert local_store.recent_events() == []
    why.assert_not_called()


def test_one_stale_ticker_is_skipped_while_the_others_alert(why, local_store):
    mixed = {**QUOTES, "AMD": q("AMD", -8.0, day="2026-10-30")}  # e.g. Finnhub's last trade is old
    with mock.patch.object(scan.market, "get_quotes", return_value=mixed):
        assert scan.run(now=NOW)["price_events"] == ["NVDA +5.0% on Nov 03"]


def test_explanation_failure_never_loses_the_alert(quotes, local_store):
    with mock.patch.object(explain, "explain_move", side_effect=RuntimeError("boom")):
        assert scan.run(now=NOW)["price_events"] == ["NVDA +5.0% on Nov 03"]
    assert "Why:" not in local_store.recent_events()[0]["body"]


def test_market_outage_does_not_crash_the_scan(local_store):
    with mock.patch.object(scan.market, "get_quotes", side_effect=scan.market.MarketDataError("down")):
        summary = scan.run(now=NOW)
    assert summary["price_events"] == [] and summary["skipped"].startswith("market data unavailable")


# ── replay (demo) ──

def test_replay_creates_one_labelled_alert_even_when_the_market_is_closed(why, local_store):
    closed = {t: {**v, "trading_date": "2026-10-30"} if "error" not in v else v for t, v in QUOTES.items()}
    closed["INTC"] = q("INTC", 0.3, 24.0, day="2026-10-30")
    saturday = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    with mock.patch.object(scan.market, "get_quotes", return_value=closed):
        first = scan.run(replay="INTC:-7.5", now=saturday)
        second = scan.run(replay="INTC:-7.5", now=saturday)
    assert first["price_events"] == ["INTC -7.5% on Oct 31 (replay)"] and second["price_events"] == []
    [event] = local_store.recent_events()
    assert event["dedupe_key"] == "replay:INTC:move_down:2026-10-31"
    prev = closed["INTC"]["prev_close"]
    assert event["body"].startswith(f"Demo replay: this move did not happen. Intel is at ${round(prev * 0.925, 2):,.2f}")
    assert "replay of yfinance" in event["body"]
    why.assert_called_once()
    item, quote, bench = why.call_args.args
    assert item["ticker"] == "INTC" and quote["change_pct"] == -7.5 and set(bench) == {"QQQ", "SPY"}


def test_replay_never_blocks_the_real_alert_and_touches_only_its_ticker(quotes, why, local_store):
    replayed = scan.run(replay="NVDA:+6", now=NOW)
    assert replayed["price_events"] == ["NVDA +6.0% on Nov 03 (replay)"]  # AMD etc. not scanned
    assert scan.run(now=NOW)["price_events"] == ["NVDA +5.0% on Nov 03"]


def test_replay_of_a_ticker_not_on_the_watchlist(quotes, why, local_store):
    with mock.patch.object(scan.market, "get_quotes", return_value={**QUOTES, "MSFT": q("MSFT", 0.1, 400.0)}):
        assert scan.run(replay="msft:-4", now=NOW)["price_events"] == ["MSFT -4.0% on Nov 03 (replay)"]


@pytest.mark.parametrize("spec", ["INTC", "INTC:abc", "INTC:0", "INTC:-80", "TOO LONG TICKER:-5"])
def test_bad_replay_specs_are_rejected(spec):
    with pytest.raises(ValueError, match="TICKER:PCT"):
        scan.parse_replay(spec)


def test_main_prints_the_summary_and_rejects_a_bad_replay(quotes, why, capsys, local_store):
    assert scan.main(["--dry-run"]) in (0,)
    assert '"dry_run": true' in capsys.readouterr().out
    assert scan.main(["--replay", "INTC"]) == 2


# ── local delivery (no Streams) ──

@pytest.fixture
def telegram_on(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")


def test_alert_is_sent_once_locally(quotes, why, local_store, telegram_on):
    with mock.patch.object(scan.telegram, "send", return_value=True) as send:
        first, second = scan.run(now=NOW), scan.run(now=NOW)
    assert first["notified"] == 1 and second["notified"] == 0
    assert send.call_count == 1 and "NVDA +5.0% on Nov 03" in send.call_args.args[0]


def test_failed_local_send_is_retried_next_run(quotes, why, local_store, telegram_on):
    with mock.patch.object(scan.telegram, "send", side_effect=[False, True]) as send:
        assert scan.run(now=NOW)["notified"] == 0
        assert scan.run(now=NOW)["notified"] == 1
    assert send.call_count == 2


def test_without_telegram_alerts_wait_in_the_feed(quotes, why, local_store):
    assert scan.run(now=NOW)["notified"] == 0
    assert local_store.recent_events()[0]["notified"] == 0


def test_no_local_delivery_when_notify_is_off_or_dry_run(quotes, why, local_store, telegram_on):
    with mock.patch.object(scan.telegram, "send", return_value=True) as send:
        scan.run(now=NOW, notify=False)
        scan.run(now=NOW, dry_run=True)
    send.assert_not_called()


def test_fail_delivery_is_only_for_a_replay():
    with pytest.raises(ValueError, match="only for a replay"):
        scan.run(fail_delivery=True)
