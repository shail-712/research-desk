"""Move explainer (F9): market check, headline filter, guarded LLM pick, scan integration.
Ported from v1 tests/unit/test_explain.py (73b6fda); quotes carry `trading_date`."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from core import explain
from core.explain import candidate_headlines as real_candidates

NOW = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
ITEM = {"ticker": "AMD", "name": "AMD", "pct_move": 3}
QUOTE = {"ticker": "AMD", "price": 466.8, "change_pct": -7.5, "trading_date": "2026-11-03"}


def bench(qqq=None, spy=None):
    return {"QQQ": {"change_pct": qqq} if qqq is not None else None,
            "SPY": {"change_pct": spy} if spy is not None else None}


def article(title, when="Tue, 03 Nov 2026 14:30:00 +0000"):
    return {"title": title, "link": "https://n", "published": when, "summary": None}


@pytest.mark.parametrize("pct, qqq, spy, expected", [
    (-4.0, -3.8, -2.9, "Moved with the market (Nasdaq-100 QQQ -3.8% the same session)."),
    (-7.5, -3.8, -2.9, None),        # 2x the market: something company-specific
    (-4.0, -1.0, -0.8, None),        # market barely moved
    (-4.0, +2.0, +1.8, None),        # market went the other way
])
def test_market_check(pct, qqq, spy, expected):
    assert explain.market_context(pct, bench(qqq, spy))[0] == expected


def test_candidates_must_be_in_the_session_window_and_name_the_company():
    feed = [article("AMD shares sink after weak data center outlook"),                      # 14:30 UTC, in session
            article("AMD premarket: chip names under pressure", "Tue, 03 Nov 2026 11:00:00 +0000"),  # pre-market counts
            article("Chip stocks slide as yields rise"),                                     # no company name
            article("AMD posts beat-and-raise report", "Tue, 03 Nov 2026 22:07:00 +0000"),   # after the close
            article("AMD unveils new GPU", "Fri, 30 Oct 2026 10:00:00 +0000"),              # too old
            article("Advanced Micro Devices cut to neutral at bank", "garbage date")]       # unparseable
    with mock.patch.object(explain.news, "get_news", return_value=feed):
        heads = real_candidates("AMD", "ADVANCED MICRO DEVICES INC", "2026-11-03", NOW)
    assert [h["title"] for h in heads] == ["AMD shares sink after weak data center outlook",
                                           "AMD premarket: chip names under pressure"]


def test_session_window_handles_mondays_and_dst():
    start, end = explain.session_window("2026-11-02", datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert start == datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)   # Fri 4 pm EDT
    assert end == datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)      # Mon 4 pm EST (DST ended Nov 1)
    _, live = explain.session_window("2026-11-03", datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc))
    assert live == datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)     # mid-session: up to now


def _with(heads, reply=None, error=None):
    patches = [mock.patch.object(explain, "candidate_headlines",
                                 return_value=[{**h, "when": NOW} for h in heads])]
    if error:
        patches.append(mock.patch.object(explain.llm, "call", side_effect=error))
    else:
        patches.append(mock.patch.object(explain.llm, "call",
                                         return_value=SimpleNamespace(text=json.dumps(reply or {}))))
    return patches


def _run(heads, reply=None, error=None, b=None):
    ps = _with(heads, reply, error)
    with ps[0], ps[1] as call:
        out = explain.explain_move(ITEM, QUOTE, b or bench(-0.4, -0.2), NOW)
    return out, call


def test_cites_the_picked_headline_verbatim_with_market_note():
    out, _ = _run([article("AMD shares sink after weak data center outlook")], {"headline_id": "h0"})
    assert out == "Likely catalyst: “AMD shares sink after weak data center outlook” (Nov 03 22:00 UTC) (market: QQQ -0.4%, SPY -0.2%)."


@pytest.mark.parametrize("reply", [{"headline_id": "none"}, {"headline_id": "h7"}])
def test_none_or_an_invented_id_means_no_catalyst(reply):
    out, _ = _run([article("AMD to present at investor conference")], reply)
    assert out.startswith(explain.NO_CATALYST)


def test_no_candidates_means_no_llm_call():
    out, call = _run([], {"headline_id": "h0"})
    assert out.startswith(explain.NO_CATALYST)
    call.assert_not_called()


def test_market_move_needs_no_headlines_or_llm():
    out, call = _run([article("AMD news")], {"headline_id": "h0"}, b=bench(-5.5, -4.0))
    assert out.startswith("Moved with the market") and "QQQ -5.5%" in out
    call.assert_not_called()


def test_llm_outage_shows_latest_headline_without_claiming_cause():
    out, _ = _run([article("AMD shares sink")], error=explain.llm.LLMUnavailableError("503"))
    assert out.startswith("Latest headline (not verified as the cause): “AMD shares sink”")


def test_after_hours_headline_explains_the_next_session_not_this_one():
    """The v1 bug (SPEC F9): an earnings headline at 16:07 New York belongs to the next session."""
    after_close = article("AMD posts beat-and-raise report", "Tue, 03 Nov 2026 21:07:00 +0000")  # 16:07 EST
    with mock.patch.object(explain.news, "get_news", return_value=[after_close]):
        today = real_candidates("AMD", "AMD", "2026-11-03", datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc))
        tomorrow = real_candidates("AMD", "AMD", "2026-11-04", datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc))
    assert today == []
    assert [h["title"] for h in tomorrow] == ["AMD posts beat-and-raise report"]
