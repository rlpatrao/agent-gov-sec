"""validationHandler Lambda — EventBridge (dataGenerator -> validator).

Validates merged JSON; on success pushes to the FIFO common queue,
on failure marks the record EXCEPTION.
"""
from __future__ import annotations

import logging
import time

from ..services import aws_clients as aws
from ..services import config as cfg
from ..services.constants import DDBStatus
from ..services.helper import handle_error, push_item_to_common_queue
from ..services.validators import validate

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:  # noqa: ARG001
    detail = event.get("detail", {})
    ddb_id = detail.get("ddbId")
    if not ddb_id:
        return {"ok": False, "reason": "missing ddbId"}

    try:
        record = aws.get_item(cfg.TABLE_NAME, {"ID": ddb_id})
        if not record:
            raise RuntimeError(f"record {ddb_id} not found")
        merged = record.get("mergedJSON") or {}
        result = validate(merged)

        if not result["success"]:
            aws.update_item(
                cfg.TABLE_NAME,
                {"ID": ddb_id},
                UpdateExpression=(
                    "SET mergedJSONValidationErrors = :e, "
                    "finalStatus = :s, #st.validated = :t"
                ),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":e": result["errors"],
                    ":s": DDBStatus.EXCEPTION.value,
                    ":t": int(time.time()),
                },
            )
            return {"ok": False, "ddbId": ddb_id, "errors": result["errors"]}

        aws.update_item(
            cfg.TABLE_NAME,
            {"ID": ddb_id},
            UpdateExpression="SET finalStatus = :s, #st.validated = :t",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s": DDBStatus.VALIDATED.value,
                ":t": int(time.time()),
            },
        )

        push_item_to_common_queue(
            service_type=record["serviceType"],
            jurisdiction=record.get("jurisdiction", ""),
            payload={"ddbId": ddb_id, **merged},
        )
        return {"ok": True, "ddbId": ddb_id}

    except Exception as exc:  # noqa: BLE001
        log.exception("validationHandler failed for %s", ddb_id)
        handle_error(ddb_id, exc)
        return {"ok": False, "ddbId": ddb_id, "error": str(exc)}
