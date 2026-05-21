"""requestCreator Lambda — HTTP POST /autograf/filing/request.

Validates the upstream event payload, stores it in AutografRequests, publishes
an EventBridge event targeting dataGenerator.
"""
from __future__ import annotations

import logging
import time
import uuid

from ..services import aws_clients as aws
from ..services import config as cfg
from ..services.constants import DDBStatus, EventType
from ..services.helper import (
    handle_error,
    json_response,
    load_event_body,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def _lookup_jurisdiction_settings(jurisdiction: str, service_type: str) -> dict:
    return aws.get_item(cfg.SETTING_TABLE_NAME,
                        {"pk": f"{jurisdiction}#{service_type}"}) or {}


def handler(event: dict, context) -> dict:  # noqa: ARG001
    body = load_event_body(event)
    event_type = body.get("eventType")
    source_system = body.get("source", "UPSTREAM")
    service_type = body.get("serviceType")
    jurisdiction = body.get("jurisdiction")

    if not service_type:
        return json_response(400, {"error": "serviceType required"})

    ddb_id = str(uuid.uuid4())

    try:
        if source_system == "UPSTREAM" and event_type == EventType.UPSTREAM.value:
            settings = _lookup_jurisdiction_settings(jurisdiction, service_type)
            if not settings.get("enabled", True):
                return json_response(
                    409, {"error": f"{jurisdiction}/{service_type} not enabled"}
                )
            if settings.get("cidPinRequired") and not body.get("cidPin"):
                return json_response(400, {"error": "CID/PIN required"})

        aws.put_item(cfg.TABLE_NAME, {
            "ID": ddb_id,
            "reqInputJSON": body,
            "serviceType": service_type,
            "jurisdiction": jurisdiction,
            "sourceSystem": source_system,
            "eventType": event_type,
            "status": {"created": int(time.time())},
            "finalStatus": DDBStatus.CREATED.value,
            "createdAt": int(time.time()),
        })

        if event_type in {EventType.MERGE_EVIDENCE.value, EventType.ORDER_RESUBMIT.value}:
            destination = "dataGenerator"
            detail = {"ddbId": ddb_id, "branch": event_type}
        else:
            destination = "dataGenerator"
            detail = {"ddbId": ddb_id}

        aws.put_event(source="requestCreator", destination=destination, detail=detail)
        return json_response(200, {"ddbId": ddb_id, "status": "CREATED"})

    except Exception as exc:  # noqa: BLE001
        log.exception("requestCreator failed")
        handle_error(ddb_id, exc)
        return json_response(500, {"error": str(exc), "ddbId": ddb_id})
