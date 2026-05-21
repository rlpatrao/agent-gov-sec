"""dataGeneratorHandler Lambda — EventBridge (requestCreator|autografTriggerer -> dataGenerator).

Fetches stored raw request, runs transformer, persists mergedJSON, publishes
event to validator.
"""
from __future__ import annotations

import logging
import time

from ..services import aws_clients as aws
from ..services import config as cfg
from ..services.constants import DDBStatus
from ..services.helper import handle_error
from ..services.transformer import TransformerFactory

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:  # noqa: ARG001
    detail = event.get("detail", {})
    ddb_id = detail.get("ddbId")
    if not ddb_id:
        log.error("dataGenerator: missing ddbId in detail %s", detail)
        return {"ok": False, "reason": "missing ddbId"}

    try:
        record = aws.get_item(cfg.TABLE_NAME, {"ID": ddb_id})
        if not record:
            raise RuntimeError(f"record {ddb_id} not found")

        merged = TransformerFactory.transform_to_filer_json(
            raw_payload=record.get("reqInputJSON", {}),
            service_type=record["serviceType"],
            jurisdiction=record.get("jurisdiction", ""),
            source_system=record.get("sourceSystem", "UPSTREAM"),
        )

        aws.update_item(
            cfg.TABLE_NAME,
            {"ID": ddb_id},
            UpdateExpression=(
                "SET mergedJSON = :m, transformedJSON = :m, "
                "#st.transformed = :t, finalStatus = :s"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":m": merged,
                ":t": int(time.time()),
                ":s": DDBStatus.TRANSFORMED.value,
            },
        )

        aws.put_event(
            source="dataGenerator",
            destination="validator",
            detail={"ddbId": ddb_id},
        )
        return {"ok": True, "ddbId": ddb_id}

    except Exception as exc:  # noqa: BLE001
        log.exception("dataGenerator failed for %s", ddb_id)
        handle_error(ddb_id, exc)
        return {"ok": False, "ddbId": ddb_id, "error": str(exc)}
