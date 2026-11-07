"""LocalStore end to end (ported from v1 tests/unit/test_store.py; Supabase tests dropped with the backend)."""

import pytest

from core import config, store as store_mod
from core.store import LocalStore


@pytest.fixture
def local_store():
    store_mod.reset_store()
    yield store_mod.get_store()
    store_mod.reset_store()


def _event(key="NVDA:move_up:2026-11-02", **kw):
    return {"ticker": "NVDA", "kind": "price_move", "title": "NVDA +5.0% today", "body": "b",
            "url": "https://x", "dedupe_key": key, **kw}


def test_local_store_is_the_default_and_seeds_the_watchlist(local_store):
    assert isinstance(local_store, LocalStore)
    assert [w["ticker"] for w in local_store.list_watchlist()] == ["NVDA", "AMD", "INTC"]


def test_watchlist_add_update_remove(local_store):
    local_store.add_ticker("AAPL", "Apple Inc.", 320193, pct_move=2.0, price_above=300)
    local_store.add_ticker("AAPL", "Apple Inc.", 320193, pct_move=4.0)  # update, not duplicate
    rows = {w["ticker"]: w for w in local_store.list_watchlist()}
    assert rows["AAPL"]["pct_move"] == 4.0 and rows["AAPL"]["price_above"] is None
    local_store.remove_ticker("NVDA")
    assert "NVDA" not in {w["ticker"] for w in local_store.list_watchlist()}


def test_removed_defaults_stay_removed_after_restart(local_store):
    local_store.remove_ticker("INTC")
    again = LocalStore(local_store.path)
    assert "INTC" not in {w["ticker"] for w in again.list_watchlist()}


def test_insert_event_dedupes_on_key(local_store):
    first = local_store.insert_event(_event())
    assert first["id"] and first["notified"] == 0
    assert local_store.insert_event(_event()) is None
    assert len(local_store.recent_events()) == 1


def test_notification_bookkeeping(local_store):
    a = local_store.insert_event(_event("a"))
    local_store.insert_event(_event("b"))
    local_store.mark_notified(a["id"])
    pending = local_store.unnotified_events("2000-01-01T00:00:00+00:00")
    assert [e["dedupe_key"] for e in pending] == ["b"]
    local_store.update_event_body(a["id"], "new body")
    assert local_store.recent_events()[-1]["body"] == "new body"


def test_state_round_trips_json(local_store):
    assert local_store.get_state("missing", 7) == 7
    local_store.set_state("k", {"accession": "1", "list": [1, 2]})
    local_store.set_state("k", {"accession": "2"})
    assert local_store.get_state("k") == {"accession": "2"}


def test_store_lives_in_the_isolated_cache_dir(local_store, tmp_path):
    assert str(local_store.path).startswith(str(tmp_path))


def test_unknown_backend_is_refused(monkeypatch):
    monkeypatch.setenv("STORE_BACKEND", "supabase")
    store_mod.reset_store()
    with pytest.raises(config.ConfigError):
        store_mod.get_store()
