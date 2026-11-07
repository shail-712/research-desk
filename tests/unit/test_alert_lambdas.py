"""Price-alert Lambdas (B.7): ScanFunction's market-hours gate and replay, NotifyFunction's Stream
handling, DLQ path and the template wiring. moto DynamoDB; Telegram and market data mocked.
Adapted from v1 tests/unit/test_lambda.py (73b6fda) scan/notify tests."""

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml
from boto3.dynamodb.types import TypeSerializer

from core import explain
from core import store as store_mod

ROOT = Path(__file__).resolve().parents[2]
IN_SESSION = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)   # Tue 13:00 New York
QUOTES = {"INTC": {"ticker": "INTC", "price": 24.0, "prev_close": 23.9, "change": 0.1, "change_pct": 0.42,
                   "trading_date": "2026-11-03", "source": "finnhub"},
          "QQQ": {"ticker": "QQQ", "price": 500.0, "change_pct": -0.4, "trading_date": "2026-11-03"},
          "SPY": {"ticker": "SPY", "price": 600.0, "change_pct": -0.2, "trading_date": "2026-11-03"}}


@pytest.fixture
def aws(dynamo_store, monkeypatch):
    monkeypatch.setattr(store_mod, "_store", dynamo_store)
    monkeypatch.delenv("SSM_PREFIX", raising=False)
    dynamo_store.seed_watchlist(store_mod.DEFAULT_WATCHLIST)
    return dynamo_store


@pytest.fixture
def scan_fn(aws):
    from aws_lambda import scan

    return importlib.reload(scan)


@pytest.fixture
def notify_fn(aws):
    from aws_lambda import notify

    return importlib.reload(notify)


@pytest.fixture
def market_and_why():
    with mock.patch("jobs.scan.market.get_quotes", return_value=dict(QUOTES)) as gq, \
         mock.patch.object(explain, "explain_move", return_value="No clear catalyst found.") as why:
        yield gq, why


# ── ScanFunction ──

@pytest.mark.parametrize("when, runs", [
    (datetime(2026, 11, 3, 14, 15, tzinfo=timezone.utc), False),   # 9:15 EST: before the open
    (datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc), True),    # 9:30 EST: the open
    (datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc), True),     # 16:00 EST: the close run
    (datetime(2026, 11, 3, 21, 15, tzinfo=timezone.utc), False),   # 16:15 EST
    (datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc), True),   # 9:30 EDT (before DST ended)
    (datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc), False),   # Saturday
])
def test_scheduled_runs_outside_the_session_are_dropped(scan_fn, market_and_why, when, runs):
    out = scan_fn.handler({"source": "schedule"}, None, now=when)
    assert ("skipped" in out and out["skipped"] == "outside market hours") is not runs


def test_two_replay_invokes_write_one_alert_and_never_send(scan_fn, market_and_why, aws, capsys):
    with mock.patch("jobs.scan.telegram.send") as send:
        first = scan_fn.handler({"replay": "INTC:-7.5"}, SimpleNamespace(aws_request_id="r1"), now=IN_SESSION)
        second = scan_fn.handler({"replay": "INTC:-7.5"}, None, now=IN_SESSION)
    assert first["price_events"] == ["INTC -7.5% on Nov 03 (replay)"] and second["price_events"] == []
    assert first["notified"] == 0
    send.assert_not_called()  # delivery is the Stream -> NotifyFunction
    [event] = aws.recent_events()
    assert event["dedupe_key"] == "replay:INTC:move_down:2026-11-03" and "fail_delivery" not in event
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"_aws"')]
    assert [e["PriceEvents"] for e in emf] == [1.0, 0.0]


def test_manual_invoke_runs_any_time_and_dry_run_writes_nothing(scan_fn, market_and_why, aws):
    saturday = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    out = scan_fn.handler({"dry_run": True, "replay": "INTC:-7.5"}, None, now=saturday)
    assert out["price_events"] == ["INTC -7.5% on Oct 31 (replay)"] and aws.recent_events() == []


def test_fail_delivery_replay_marks_the_feed_item(scan_fn, market_and_why, aws):
    scan_fn.handler({"replay": "INTC:-7.5", "fail_delivery": True}, None, now=IN_SESSION)
    [event] = aws.recent_events()
    assert event["fail_delivery"] is True and event["dedupe_key"].endswith(":fail")


# ── NotifyFunction ──

_ser = TypeSerializer()


def stream_record(item: dict, seq: str, event_name="INSERT") -> dict:
    return {"eventName": event_name,
            "dynamodb": {"SequenceNumber": seq, "NewImage": {k: _ser.serialize(v) for k, v in item.items()}}}


def feed_item(aws, key="k", **extra):
    row = aws.insert_event({"ticker": "INTC", "kind": "price_move", "title": f"INTC -7.5% {key}",
                            "body": "b", "dedupe_key": key, **extra})
    return aws.table.get_item(Key={"pk": "FEED", "sk": row["id"]})["Item"]


@pytest.fixture
def telegram_on(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc-token-for-tests")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")


def test_notify_sends_and_marks(notify_fn, aws, telegram_on, capsys):
    item = feed_item(aws)
    with mock.patch.object(notify_fn.telegram, "send", return_value=True) as send:
        out = notify_fn.handler({"Records": [stream_record(item, "1")]}, None)
    assert out == {"batchItemFailures": []}
    assert "INTC -7.5% k" in send.call_args.args[0]
    assert aws.recent_events()[0]["notified"] is True
    assert '"AlertsSent": 1.0' in capsys.readouterr().out


def test_notify_stops_at_the_first_failure_for_a_clean_retry(notify_fn, aws, telegram_on):
    a, b = feed_item(aws, "a"), feed_item(aws, "b")
    with mock.patch.object(notify_fn.telegram, "send", side_effect=[False, True]) as send:
        out = notify_fn.handler({"Records": [stream_record(a, "1"), stream_record(b, "2")]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "1"}]}
    assert send.call_count == 1  # b is redelivered with the retry, so it isn't sent twice


def test_fail_delivery_item_is_reported_failed_without_calling_telegram(notify_fn, aws, telegram_on):
    item = feed_item(aws, "demo", fail_delivery=True)
    with mock.patch.object(notify_fn.telegram, "send") as send:
        out = notify_fn.handler({"Records": [stream_record(item, "9")]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "9"}]}
    send.assert_not_called()


def test_unconfigured_telegram_is_a_failure_so_the_alert_reaches_the_dlq(notify_fn, aws, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    item = feed_item(aws)
    out = notify_fn.handler({"Records": [stream_record(item, "5")]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "5"}]}


def test_notify_skips_non_feed_and_already_notified_records(notify_fn, aws, telegram_on):
    records = [stream_record({"pk": "JOB", "sk": "x"}, "1"),
               stream_record({"pk": "FEED", "sk": "y", "notified": True}, "2"),
               stream_record({"pk": "FEED", "sk": "z"}, "3", event_name="MODIFY")]
    with mock.patch.object(notify_fn.telegram, "send") as send:
        assert notify_fn.handler({"Records": records}, None) == {"batchItemFailures": []}
    send.assert_not_called()


def test_a_bookkeeping_failure_after_sending_is_not_resent(notify_fn, aws, telegram_on):
    item = feed_item(aws)
    with mock.patch.object(notify_fn.telegram, "send", return_value=True), \
         mock.patch.object(aws, "mark_notified", side_effect=store_mod.StoreError("Database error (x)")):
        assert notify_fn.handler({"Records": [stream_record(item, "1")]}, None) == {"batchItemFailures": []}


# ── template wiring ──

class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: {
    f"!{suffix}": loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node, deep=True)})


@pytest.fixture(scope="module")
def template():
    return yaml.load((ROOT / "template.yaml").read_text(encoding="utf-8"), Loader=_CfnLoader)


def test_scheduler_runs_in_new_york_time_and_tags_its_runs(template):
    props = template["Resources"]["ScanFunction"]["Properties"]
    sched = props["Events"]["MarketHours"]
    assert sched["Type"] == "ScheduleV2"
    assert sched["Properties"]["ScheduleExpression"] == "cron(*/15 9-16 ? * MON-FRI *)"
    assert sched["Properties"]["ScheduleExpressionTimezone"] == "America/New_York"
    assert json.loads(sched["Properties"]["Input"]) == {"source": "schedule"}
    assert "s3" not in json.dumps(props["Policies"]).lower()  # prices only: no index access


def test_stream_filter_dlq_and_alarm_are_wired(template):
    res = template["Resources"]
    event = res["NotifyFunction"]["Properties"]["Events"]["NewAlerts"]["Properties"]
    pattern = json.loads(event["FilterCriteria"]["Filters"][0]["Pattern"])
    assert pattern == {"eventName": ["INSERT"], "dynamodb": {"NewImage": {"pk": {"S": ["FEED"]}}}}
    assert event["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert event["DestinationConfig"]["OnFailure"]["Destination"] == {"!GetAtt": "NotifyDLQ.Arn"}
    assert 0 < event["MaximumRetryAttempts"] <= 3
    alarm = res["FailedAlertsAlarm"]["Properties"]
    assert alarm["Dimensions"][0]["Value"] == {"!GetAtt": "NotifyDLQ.QueueName"}
    assert alarm["Threshold"] == 0 and alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["AlarmActions"] == [{"!Ref": "AlarmTopic"}]
    statements = [s for p in res["NotifyFunction"]["Properties"]["Policies"]
                  if isinstance(p, dict) and isinstance(p.get("Statement"), list) for s in p["Statement"]]
    ddb = [s for s in statements if any(a.startswith("dynamodb:") for a in s["Action"])]
    assert ddb == [{"Effect": "Allow", "Action": ["dynamodb:UpdateItem"], "Resource": {"!GetAtt": "Table.Arn"}}]
