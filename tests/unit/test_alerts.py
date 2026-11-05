"""Price alert rules. Adapted from v1 tests/unit/test_alerts.py (73b6fda): `trading_date`, and the
no-session rule (D12)."""

from datetime import datetime, timezone

import pytest

from core.alerts import is_current, new_york_today, price_events

TODAY = "2026-11-02"
QUOTE = {"ticker": "NVDA", "price": 210.0, "prev_close": 200.0, "change": 10.0, "change_pct": 5.0,
         "trading_date": TODAY, "source": "finnhub"}


def item(**rules):
    return {"ticker": "NVDA", "name": "NVIDIA", "pct_move": 3.0, "price_above": None,
            "price_below": None, **rules}


def test_big_move_fires_once_per_direction_per_day():
    events = price_events(item(), QUOTE, TODAY)
    assert len(events) == 1
    assert events[0]["kind"] == "price_move"
    assert events[0]["title"] == "NVDA +5.0% on Nov 02"
    assert events[0]["dedupe_key"] == "NVDA:move_up:2026-11-02"
    assert "(alert threshold ±3%; quote: finnhub)" in events[0]["body"]
    down = price_events(item(), {**QUOTE, "change": -10.0, "change_pct": -5.0}, TODAY)
    assert down[0]["dedupe_key"] == "NVDA:move_down:2026-11-02"


def test_small_move_is_quiet():
    assert price_events(item(pct_move=6), QUOTE, TODAY) == []


def test_missing_threshold_uses_default_three_percent():
    assert len(price_events(item(pct_move=None), {**QUOTE, "change_pct": 3.2}, TODAY)) == 1
    assert price_events(item(pct_move=None), {**QUOTE, "change_pct": 2.9}, TODAY) == []


def test_target_crossings():
    events = price_events(item(pct_move=50, price_above=205, price_below=None), QUOTE, TODAY)
    assert [e["kind"] for e in events] == ["price_above"]
    assert events[0]["dedupe_key"] == "NVDA:above:205:2026-11-02"
    events = price_events(item(pct_move=50, price_below="215"), QUOTE, TODAY)
    assert [e["kind"] for e in events] == ["price_below"]


@pytest.mark.parametrize("quote", [None, {}, {"ticker": "NVDA", "error": "no data"}, {**QUOTE, "price": None}])
def test_no_data_means_no_alerts(quote):
    assert price_events(item(), quote, TODAY) == []


@pytest.mark.parametrize("trading_date", ["2026-10-30", None, ""])
def test_stale_or_holiday_quote_means_no_alerts(trading_date):
    """D12: a quote from another session (holiday, weekend, stale) never fires, even a big move."""
    quote = {**QUOTE, "trading_date": trading_date, "change_pct": -9.0}
    assert not is_current(quote, TODAY)
    assert price_events(item(price_above=1, price_below=1000), quote, TODAY) == []


def test_today_is_the_new_york_date():
    # 02:30 IST on Nov 3 = 21:00 UTC on Nov 2 = 16:00 New York on Nov 2.
    assert new_york_today(datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)) == "2026-11-02"
    assert new_york_today(datetime(2026, 11, 3, 4, 59, tzinfo=timezone.utc)) == "2026-11-02"
    assert new_york_today(datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)) == "2026-11-03"
