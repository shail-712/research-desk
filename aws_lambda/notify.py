"""
DynamoDB Streams -> Telegram.

The event source mapping (template.yaml) filters the stream to INSERTs of FEED items, so this
only sees new alerts. Each record is sent, then marked notified. A failed send is reported in
`batchItemFailures`, so Lambda retries from that record; after the retry limit the record goes to
the SQS dead-letter queue, which has a CloudWatch alarm (SPEC F12).

An item written with `fail_delivery` (only by a manual replay, see aws_lambda/scan.py) is treated
as a failed send without calling Telegram: the demo of the DLQ path.

Ported from v1 aws_lambda/notify.py (73b6fda): request ID per record, the failure demo, an
unconfigured Telegram is a failure (it was silently skipped, so alerts were never delivered).
"""

import logging

from boto3.dynamodb.types import TypeDeserializer

from aws_lambda.runtime import bootstrap, emit_metrics

bootstrap()

from core import log, telegram  # noqa: E402
from core.store import StoreError, get_store  # noqa: E402

logger = logging.getLogger(__name__)
_deserializer = TypeDeserializer()


def _new_image(record) -> dict:
    image = record.get("dynamodb", {}).get("NewImage", {})
    return {k: _deserializer.deserialize(v) for k, v in image.items()}


def handler(event, context):
    log.new_request_id(getattr(context, "aws_request_id", None))
    failures, sent, skipped = [], 0, 0
    store = get_store()
    for record in event.get("Records", []):
        item = _new_image(record)
        if record.get("eventName") != "INSERT" or item.get("pk") != "FEED" or item.get("notified"):
            skipped += 1
            continue
        if item.get("fail_delivery"):
            ok = False
            logger.warning("Alert %s marked fail_delivery (demo): reporting a failed send", item.get("sk"))
        elif not telegram.is_configured():
            ok = False
            logger.error("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in SSM)")
        else:
            ok = telegram.send(telegram.format_event(item))
        if not ok:
            # Streams retry from the first failed record onward, so stop here: records after it
            # would be redelivered anyway, and sending them now would double them.
            failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
            break
        sent += 1
        try:
            store.mark_notified(item["sk"])
        except StoreError as e:  # already delivered: don't resend over a bookkeeping failure
            logger.warning("Sent but not marked notified: %s", e)
    emit_metrics({"AlertsSent": sent, "AlertSendFailures": len(failures)},
                 units={"AlertsSent": "Count", "AlertSendFailures": "Count"})
    logger.info("Notify: sent=%d failed=%d skipped=%d", sent, len(failures), skipped,
                extra={"sent": sent, "failed": len(failures), "skipped": skipped})
    return {"batchItemFailures": failures}
