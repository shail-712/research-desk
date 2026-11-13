"""
EventBridge Scheduler -> the price scan (jobs/scan.py).

The schedule is `cron(*/15 9-16 ? * MON-FRI *)` in America/New_York (template.yaml), which also
fires at 9:00, 9:15 and 16:15-16:45; scheduled runs (`{"source": "schedule"}`) outside the 9:30-16:00
session are dropped here, so DST never needs a second cron. Manual invokes run at any time:

    {"dry_run": true}                     what would fire, nothing written
    {"replay": "INTC:-7.5"}               demo replay (PLAN B): one labelled alert, deduped
    {"replay": "INTC:-7.5", "fail_delivery": true}
                                          demo of the failure path: NotifyFunction refuses that
                                          alert, so it lands in the DLQ and the alarm fires.
                                          Refused during the New York session (review I3,
                                          finding 1): a failed record takes the rest of its
                                          stream batch to the DLQ, so a real alert written in
                                          the same poll would never be sent.

Alerts are only WRITTEN here (DynamoDB, transactional dedupe). Delivery is NotifyFunction via
DynamoDB Streams, so a Telegram outage can't slow or fail the scan.

Adapted from v1 aws_lambda/scan.py (73b6fda): prices only (D13), market-hours gate, replay.
"""

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from aws_lambda.runtime import bootstrap, emit_metrics

bootstrap()

from core import log  # noqa: E402
from jobs import scan  # noqa: E402

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)


def in_session(now: datetime | None = None) -> bool:
    """Mon-Fri 9:30-16:00 New York (the 16:00 run catches the close). Holidays: D12."""
    ny = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    return ny.weekday() < 5 and SESSION_OPEN <= ny.time().replace(second=0, microsecond=0) <= SESSION_CLOSE


def handler(event, context, now: datetime | None = None):
    event = event or {}
    log.new_request_id(getattr(context, "aws_request_id", None))
    if event.get("source") == "schedule" and not in_session(now):
        logger.info("Outside the New York session; scheduled run dropped")
        return {"skipped": "outside market hours"}
    if event.get("fail_delivery") and in_session(now):
        logger.warning("fail_delivery refused during the New York session (it could take real alerts "
                       "in the same stream batch to the DLQ); run it outside 9:30-16:00 ET")
        return {"refused": "fail_delivery only outside market hours (Mon-Fri 9:30-16:00 New York)"}
    summary = scan.run(dry_run=bool(event.get("dry_run")), replay=event.get("replay"),
                       fail_delivery=bool(event.get("fail_delivery")), now=now, notify=False)
    emit_metrics({"ScanSeconds": summary["seconds"], "PriceEvents": len(summary["price_events"])},
                 units={"ScanSeconds": "Seconds", "PriceEvents": "Count"})
    return summary
