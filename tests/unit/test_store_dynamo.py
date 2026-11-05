"""DynamoStore against moto's in-memory DynamoDB: single-table design, transactional dedupe.
Ported from v1 tests/unit/test_store_dynamo.py (73b6fda)."""


import pytest

from core import store as store_mod
from core.store import StoreError


def _event(key="NVDA:move_up:2026-11-02", **kw):
    return {"ticker": "NVDA", "kind": "price_move", "title": "NVDA +5.0% on Nov 02", "body": "b",
            "url": "https://x", "dedupe_key": key, **kw}


def test_watchlist_upsert_keeps_added_at_and_handles_numbers(dynamo_store):
    dynamo_store.add_ticker("NVDA", "NVIDIA", 1045810)
    dynamo_store.add_ticker("AAPL", "Apple Inc.", 320193, pct_move=2.5, price_above=300.5)
    first = {w["ticker"]: w for w in dynamo_store.list_watchlist()}
    dynamo_store.add_ticker("AAPL", "Apple Inc.", 320193, pct_move=4.0)  # update
    rows = {w["ticker"]: w for w in dynamo_store.list_watchlist()}
    assert rows["AAPL"]["pct_move"] == 4.0 and rows["AAPL"]["price_above"] is None
    assert rows["AAPL"]["added_at"] == first["AAPL"]["added_at"]
    assert rows["NVDA"]["cik"] == 1045810 and isinstance(rows["NVDA"]["cik"], int)
    dynamo_store.remove_ticker("NVDA")
    assert [w["ticker"] for w in dynamo_store.list_watchlist()] == ["AAPL"]


def test_seed_only_when_empty(dynamo_store):
    dynamo_store.seed_watchlist(store_mod.DEFAULT_WATCHLIST)
    dynamo_store.remove_ticker("INTC")
    dynamo_store.seed_watchlist(store_mod.DEFAULT_WATCHLIST)
    assert [w["ticker"] for w in dynamo_store.list_watchlist()] == ["NVDA", "AMD"]


def test_insert_event_is_transactionally_deduped(dynamo_store):
    first = dynamo_store.insert_event(_event())
    assert first["id"].startswith(first["created_at"]) and first["notified"] is False
    assert dynamo_store.insert_event(_event()) is None
    assert len(dynamo_store.recent_events()) == 1
    # the marker and the feed item are both there
    marker = dynamo_store.table.get_item(Key={"pk": "SEEN#NVDA:move_up:2026-11-02", "sk": "-"})["Item"]
    assert marker["feed_sk"] == first["id"]


def test_dedupe_is_one_conditional_transaction(dynamo_store):
    """Two racing scans can't both record an alert: the SEEN marker's `attribute_not_exists`
    condition and the FEED put are ONE TransactWriteItems, so DynamoDB applies both or neither.
    (moto doesn't serialise concurrent transactions, so a threaded race test is flaky locally;
    this checks the request the guarantee rests on.)"""
    from unittest import mock

    real = dynamo_store.client.transact_write_items
    with mock.patch.object(dynamo_store.client, "transact_write_items", side_effect=real) as tx:
        dynamo_store.insert_event(_event("race"))
    [items] = [c.kwargs["TransactItems"] for c in tx.call_args_list]
    marker, feed = items[0]["Put"], items[1]["Put"]
    assert marker["Item"]["pk"] == "SEEN#race" and marker["ConditionExpression"] == "attribute_not_exists(pk)"
    assert feed["Item"]["pk"] == "FEED" and marker["Item"]["feed_sk"] == feed["Item"]["sk"]
    assert dynamo_store.insert_event(_event("race")) is None
    assert len(dynamo_store.recent_events()) == 1


def test_feed_newest_first_with_ttl_and_no_internal_keys(dynamo_store):
    for i in range(3):
        dynamo_store.insert_event(_event(f"k{i}", title=f"t{i}"))
    events = dynamo_store.recent_events(2)
    assert [e["title"] for e in events] == ["t2", "t1"]
    assert "pk" not in events[0] and "expires_at" not in events[0]
    raw = dynamo_store.table.get_item(Key={"pk": "FEED", "sk": events[0]["id"]})["Item"]
    assert raw["expires_at"] > 0


def test_notification_bookkeeping(dynamo_store):
    a = dynamo_store.insert_event(_event("a"))
    dynamo_store.insert_event(_event("b"))
    dynamo_store.mark_notified(a["id"])
    pending = dynamo_store.unnotified_events("2000-01-01T00:00:00+00:00")
    assert [e["dedupe_key"] for e in pending] == ["b"]
    dynamo_store.update_event_body(a["id"], "new body")
    assert {e["dedupe_key"]: e["body"] for e in dynamo_store.recent_events()}["a"] == "new body"
    with pytest.raises(StoreError):
        dynamo_store.mark_notified("missing")


def test_state_round_trip(dynamo_store):
    assert dynamo_store.get_state("x", []) == []
    dynamo_store.set_state("x", {"queue": [{"accession": "1"}], "n": 1.5})
    assert dynamo_store.get_state("x") == {"queue": [{"accession": "1"}], "n": 1.5}


def test_async_job_lifecycle(dynamo_store):
    job_id = dynamo_store.create_job("Why did AMD fall?", "deep")
    assert dynamo_store.get_job(job_id)["status"] == "pending"
    dynamo_store.finish_job(job_id, "done", {"answer": "A [E1].", "timings": {"total": 9.5}})
    job = dynamo_store.get_job(job_id)
    assert job["status"] == "done" and job["result"]["timings"]["total"] == 9.5
    assert dynamo_store.get_job("nope") is None


def test_get_store_selects_dynamodb(monkeypatch, aws_env):
    monkeypatch.setenv("STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("DDB_TABLE", "ResearchDesk")
    store_mod.reset_store()
    assert store_mod.get_store().name == "DynamoDB"


def test_markers_and_jobs_expire(dynamo_store):
    """TTL attributes on SEEN markers (90 d) and jobs (1 d), so the table doesn't grow forever."""
    import time

    dynamo_store.insert_event(_event("ttl"))
    marker = dynamo_store.table.get_item(Key={"pk": "SEEN#ttl", "sk": "-"})["Item"]
    assert 89 * 86400 < int(marker["expires_at"]) - time.time() <= 90 * 86400
    job_id = dynamo_store.create_job("q", "quick")
    raw = dynamo_store.table.get_item(Key={"pk": "JOB", "sk": job_id})["Item"]
    assert 0 < int(raw["expires_at"]) - time.time() <= 86400
    assert "expires_at" not in dynamo_store.get_job(job_id)


def test_missing_table_is_a_store_error_with_a_safe_message(aws_env):
    import boto3
    from moto import mock_aws

    from core.store_dynamo import DynamoStore

    with mock_aws():
        store = DynamoStore("NoSuchTable", resource=boto3.resource("dynamodb", region_name="ap-south-1"))
        with pytest.raises(StoreError, match=r"^Database error \(ResourceNotFoundException\)$"):
            store.list_watchlist()
        with pytest.raises(StoreError):
            store.insert_event(_event())


def test_deep_daily_counter_works_on_dynamodb(dynamo_store, monkeypatch):
    """core.agents.deep keeps its Pacific-day counter in STATE through the same interface."""
    from core.agents import deep

    monkeypatch.setattr(store_mod, "_store", dynamo_store)
    monkeypatch.setattr(deep, "DEEP_MODE_DAILY_CAP", 2)
    assert [deep._take_daily_slot() for _ in range(3)] == [True, True, False]
    assert dynamo_store.get_state(deep._cap_key()) == 2
