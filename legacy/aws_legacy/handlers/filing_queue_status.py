"""filingQueueStatusHandler Lambda — SQS trigger (AutografCommonQueue_local.fifo).

Enforces bot concurrency: if idle -> mark IN_PROGRESS, decrement counter,
publish filer event. If busy -> leave message for later.
Delaware COGS bypasses the SQS flow.
"""
from __future__ import annotations

import json
import logging

from ..services import aws_clients as aws
from ..services.constants import BotStatus
from ..services.helper import (
    alter_filing_count,
    handle_error,
    record_filing_started_if_idle,
    update_bot_filing_status,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def _process(record: dict) -> dict:
    body = record.get("body", "{}")
    try:
        msg = json.loads(body)
    except json.JSONDecodeError:
        log.error("malformed SQS body: %r", body)
        return {"ok": False, "reason": "bad_body"}

    service_type = msg.get("serviceType")
    jurisdiction = msg.get("jurisdiction")
    payload = msg.get("payload", {})
    ddb_id = payload.get("ddbId", "")

    try:
        if jurisdiction == "DELAWARE" and service_type == "COGS":
            aws.put_event(source="filingQueueStatusHandler", destination="filer",
                          detail={"ddbId": ddb_id, "payload": payload})
            return {"ok": True, "bypass": "DELAWARE_COGS"}

        if not record_filing_started_if_idle(jurisdiction, service_type):
            log.info("bot busy for %s/%s — leaving message", jurisdiction, service_type)
            raise RuntimeError("bot_busy")  # triggers SQS retry/visibility

        update_bot_filing_status(
            jurisdiction, service_type, BotStatus.IN_PROGRESS,
            data={"ddbId": ddb_id},
        )
        alter_filing_count(jurisdiction, service_type, -1)

        aws.put_event(
            source="filingQueueStatusHandler",
            destination="filer",
            detail={"ddbId": ddb_id, "payload": payload,
                    "serviceType": service_type, "jurisdiction": jurisdiction},
        )
        return {"ok": True, "ddbId": ddb_id}

    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("filingQueueStatusHandler failed")
        if ddb_id:
            handle_error(ddb_id, exc)
        return {"ok": False, "error": str(exc)}


def handler(event: dict, context) -> dict:  # noqa: ARG001
    results = []
    failed_ids: list[dict] = []
    for record in event.get("Records", []):
        try:
            results.append(_process(record))
        except RuntimeError:
            failed_ids.append({"itemIdentifier": record.get("messageId")})
    return {"batchItemFailures": failed_ids, "results": results}
