"""
DynamoDB backend for the store (STORE_BACKEND=dynamodb): one table, the single-table design in SPEC §4.

    pk                  sk                    item
    WATCH               TICKER#NVDA           watchlist row + alert rules
    FEED                <created_at>#<id>     alert event            (TTL 30 d, Stream source)
    SEEN#<dedupe_key>   -                     dedupe marker          (TTL 90 d)
    STATE               <key>                 small job state (JSON string): Deep daily counter, ...
    JOB                 <job_id>              async /ask job         (TTL 1 d)

Dedupe is one TransactWriteItems: put the SEEN marker only if it doesn't exist AND put the FEED
item. Either both land or neither does, so an alert can never be recorded twice, even with two
scans racing.

Same interface as LocalStore (core/store.py), plus the async job methods the API uses. Vectors
live on S3 (core/vectors.py).

Ported from v1 core/store_dynamo.py (73b6fda): `supports_vectors` dropped with pgvector (D4).
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from core.store import DEFAULT_PCT_MOVE, StoreError

logger = logging.getLogger(__name__)

FEED_TTL_DAYS = 30
SEEN_TTL_DAYS = 90
JOB_TTL_HOURS = 24


def _now_iso() -> str:
    # Microseconds keep FEED sort keys and watchlist order stable within one second.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _expires(days: float) -> int:
    return int(time.time() + days * 86400)


def _to_ddb(value):
    """Python -> DynamoDB-safe values (floats become Decimal)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value


def _from_ddb(value):
    """DynamoDB values -> plain Python (Decimal becomes int or float)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value


def _event_row(item: dict) -> dict:
    row = _from_ddb(item)
    row["id"] = row.pop("sk")
    for internal in ("pk", "expires_at"):
        row.pop(internal, None)
    return row


class DynamoStore:
    name = "DynamoDB"

    def __init__(self, table_name: str, region: str | None = None, resource=None):
        self.table_name = table_name
        resource = resource or boto3.resource("dynamodb", region_name=region)
        self.table = resource.Table(table_name)
        self.client = self.table.meta.client

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            logger.error("DynamoDB %s failed: %s", getattr(fn, "__name__", "call"), code)
            raise StoreError(f"Database error ({code})") from e
        except BotoCoreError as e:
            raise StoreError(f"Database unreachable ({type(e).__name__})") from e

    # ── watchlist ──
    def list_watchlist(self) -> list[dict]:
        items = self._call(self.table.query, KeyConditionExpression=Key("pk").eq("WATCH"))["Items"]
        rows = [_from_ddb({k: v for k, v in i.items() if k not in ("pk", "sk")}) for i in items]
        return sorted(rows, key=lambda r: r.get("added_at", ""))

    def add_ticker(self, ticker: str, name: str | None = None, cik: int | None = None,
                   pct_move: float = DEFAULT_PCT_MOVE, price_above=None, price_below=None) -> None:
        self._call(
            self.table.update_item,
            Key={"pk": "WATCH", "sk": f"TICKER#{ticker}"},
            UpdateExpression=("SET ticker = :t, #n = :n, cik = :c, pct_move = :p, price_above = :a, "
                              "price_below = :b, added_at = if_not_exists(added_at, :now)"),
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues=_to_ddb({
                ":t": ticker, ":n": name or ticker, ":c": cik, ":p": float(pct_move),
                ":a": None if price_above is None else float(price_above),
                ":b": None if price_below is None else float(price_below), ":now": _now_iso()}),
        )

    def remove_ticker(self, ticker: str) -> None:
        self._call(self.table.delete_item, Key={"pk": "WATCH", "sk": f"TICKER#{ticker}"})

    def seed_watchlist(self, rows: list[dict]) -> None:
        """Adds the defaults once (used by the bootstrap script)."""
        if not self.list_watchlist():
            for r in rows:
                self.add_ticker(r["ticker"], r.get("name"), r.get("cik"))

    # ── events ──
    def insert_event(self, event: dict) -> dict | None:
        created = _now_iso()
        sk = f"{created}#{uuid.uuid4().hex[:8]}"
        item = _to_ddb({
            "pk": "FEED", "sk": sk, "ticker": event["ticker"], "kind": event["kind"],
            "title": event["title"], "body": event.get("body"), "url": event.get("url"),
            "dedupe_key": event["dedupe_key"], "notified": False, "created_at": created,
            "expires_at": _expires(FEED_TTL_DAYS),
        })
        if event.get("fail_delivery"):  # demo replay of the DLQ path (aws_lambda/notify.py)
            item["fail_delivery"] = True
        marker = {"pk": f"SEEN#{event['dedupe_key']}", "sk": "-", "feed_sk": sk,
                  "expires_at": _expires(SEEN_TTL_DAYS)}
        try:
            # The resource's client serializes plain Python items itself.
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table_name, "Item": _to_ddb(marker),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": {"TableName": self.table_name, "Item": item}},
            ])
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
                if "ConditionalCheckFailed" in reasons or not reasons:
                    return None  # already recorded
            logger.error("DynamoDB transaction failed: %s", e.response.get("Error", {}).get("Code"))
            raise StoreError("Database error (transaction)") from e
        except BotoCoreError as e:
            raise StoreError(f"Database unreachable ({type(e).__name__})") from e
        return _event_row(item)

    def has_event(self, dedupe_key: str) -> bool:
        return "Item" in self._call(self.table.get_item, Key={"pk": f"SEEN#{dedupe_key}", "sk": "-"})

    def recent_events(self, limit: int = 20) -> list[dict]:
        items = self._call(self.table.query, KeyConditionExpression=Key("pk").eq("FEED"),
                           ScanIndexForward=False, Limit=limit)["Items"]
        return [_event_row(i) for i in items]

    def unnotified_events(self, since_iso: str) -> list[dict]:
        items = self._call(self.table.query,
                           KeyConditionExpression=Key("pk").eq("FEED") & Key("sk").gte(since_iso),
                           FilterExpression=Attr("notified").eq(False))["Items"]
        return [_event_row(i) for i in items]

    def mark_notified(self, event_id) -> None:
        self._call(self.table.update_item, Key={"pk": "FEED", "sk": event_id},
                   UpdateExpression="SET notified = :t", ExpressionAttributeValues={":t": True},
                   ConditionExpression="attribute_exists(pk)")

    def update_event_body(self, event_id, body: str) -> None:
        self._call(self.table.update_item, Key={"pk": "FEED", "sk": event_id},
                   UpdateExpression="SET body = :b", ExpressionAttributeValues={":b": body},
                   ConditionExpression="attribute_exists(pk)")

    # ── state ──
    def get_state(self, key: str, default=None):
        item = self._call(self.table.get_item, Key={"pk": "STATE", "sk": key}).get("Item")
        return json.loads(item["value"]) if item else default

    def set_state(self, key: str, value) -> None:
        self._call(self.table.put_item, Item={"pk": "STATE", "sk": key, "value": json.dumps(value),
                                              "updated_at": _now_iso()})

    # ── async ask jobs ──
    def create_job(self, question: str, mode: str) -> str:
        job_id = uuid.uuid4().hex
        self._call(self.table.put_item, Item={
            "pk": "JOB", "sk": job_id, "status": "pending", "question": question, "mode": mode,
            "created_at": _now_iso(), "expires_at": _expires(JOB_TTL_HOURS / 24)})
        return job_id

    def finish_job(self, job_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        self._call(self.table.update_item, Key={"pk": "JOB", "sk": job_id},
                   UpdateExpression="SET #s = :s, #r = :r, #e = :e, finished_at = :f",
                   ExpressionAttributeNames={"#s": "status", "#r": "result", "#e": "error"},
                   ExpressionAttributeValues={":s": status, ":r": json.dumps(result) if result else None,
                                              ":e": error, ":f": _now_iso()})

    def get_job(self, job_id: str) -> dict | None:
        item = self._call(self.table.get_item, Key={"pk": "JOB", "sk": job_id}).get("Item")
        if not item:
            return None
        job = _from_ddb(item)
        job["job_id"] = job.pop("sk")
        job.pop("pk", None)
        job.pop("expires_at", None)
        job["result"] = json.loads(job["result"]) if job.get("result") else None
        return job


# Mirrors the table in template.yaml (for tests and local development).
TABLE_DEFINITION = {
    "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
    "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"},
                             {"AttributeName": "sk", "AttributeType": "S"}],
    "ProvisionedThroughput": {"ReadCapacityUnits": 2, "WriteCapacityUnits": 2},
    "StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
}


def create_table(resource, name: str):
    table = resource.create_table(TableName=name, **TABLE_DEFINITION)
    table.wait_until_exists()
    return table
