"""Shared helpers — port of `src/helper.ts`."""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import requests

from . import aws_clients as aws
from . import config as cfg
from .constants import BotStatus, DDBStatus

log = logging.getLogger(__name__)


# ---------- Slack / error ----------
def send_slack_notification(message: str, context: dict | None = None) -> None:
    if not cfg.SLACK_WEBHOOK_URL:
        log.info("slack: %s %s", message, context or {})
        return
    payload = {"text": message}
    if context:
        payload["text"] += "\n```" + json.dumps(context, default=str) + "```"
    try:
        requests.post(cfg.SLACK_WEBHOOK_URL, json=payload, timeout=5)
    except requests.RequestException:
        log.exception("Slack notification failed")


def handle_error(ddb_id: str, error: Exception, *, upstream_callback_url: str | None = None) -> None:
    """Mark a DynamoDB record EXCEPTION, notify Slack, optionally call upstream."""
    try:
        aws.update_item(
            cfg.TABLE_NAME,
            {"ID": ddb_id},
            UpdateExpression="SET finalStatus = :s, errorDetails = :e, updatedAt = :t",
            ExpressionAttributeValues={
                ":s": DDBStatus.EXCEPTION.value,
                ":e": str(error),
                ":t": int(time.time()),
            },
        )
    except Exception:  # pragma: no cover - best effort
        log.exception("Failed to mark EXCEPTION on %s", ddb_id)

    send_slack_notification(f":rotating_light: Filing exception ({ddb_id})",
                            {"error": str(error)})

    if upstream_callback_url:
        try:
            requests.post(upstream_callback_url, json={"ddbId": ddb_id, "status": "FAILED",
                                                     "error": str(error)}, timeout=5)
        except requests.RequestException:
            log.exception("Upstream callback failed")


def handle_error_dlq(record: dict) -> None:
    """DLQ-specific error handler: marks the order as a timeout failure."""
    body = _parse_body(record)
    ddb_id = body.get("ddbId") or body.get("ID")
    if not ddb_id:
        log.warning("DLQ record missing ddbId: %s", record)
        return
    aws.update_item(
        cfg.TABLE_NAME,
        {"ID": ddb_id},
        UpdateExpression="SET finalStatus = :s, errorDetails = :e, updatedAt = :t",
        ExpressionAttributeValues={
            ":s": DDBStatus.EXCEPTION.value,
            ":e": "DLQ timeout",
            ":t": int(time.time()),
        },
    )
    send_slack_notification(f":warning: DLQ failure for {ddb_id}")


def _parse_body(record: dict) -> dict:
    body = record.get("body") or record.get("Body") or "{}"
    try:
        return json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        return {}


# ---------- Common queue ----------
def create_sqs_payload(service_type: str, jurisdiction: str, payload: dict) -> dict:
    return {
        "serviceType": service_type,
        "jurisdiction": jurisdiction,
        "payload": payload,
        "enqueuedAt": int(time.time()),
    }


def push_item_to_common_queue(service_type: str, jurisdiction: str, payload: dict) -> None:
    body = create_sqs_payload(service_type, jurisdiction, payload)
    group_id = f"{jurisdiction}#{service_type}"
    aws.sqs_send_message(cfg.SQS_QUEUE_URL, body, group_id=group_id,
                         dedup_id=str(uuid.uuid4()))


# ---------- Bot status ----------
def _bot_status_key(jurisdiction: str, service_type: str) -> dict:
    return {"pk": f"BOT_STATUS#{jurisdiction}#{service_type}"}


def update_bot_filing_status(jurisdiction: str, service_type: str,
                             status: BotStatus, *, data: dict | None = None) -> None:
    expr = "SET #s = :s, updatedAt = :t"
    values: dict[str, Any] = {":s": status.value, ":t": int(time.time())}
    names = {"#s": "status"}
    if status == BotStatus.IN_PROGRESS:
        expr += ", startTime = :st"
        values[":st"] = int(time.time())
    if data is not None:
        expr += ", #d = :d"
        names["#d"] = "data"
        values[":d"] = data
    aws.update_item(
        cfg.SETTING_TABLE_NAME,
        _bot_status_key(jurisdiction, service_type),
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def record_filing_started_if_idle(jurisdiction: str, service_type: str) -> bool:
    """Conditionally mark bot IN_PROGRESS iff it's currently IDLE. Returns True on success."""
    try:
        aws.update_item(
            cfg.SETTING_TABLE_NAME,
            _bot_status_key(jurisdiction, service_type),
            UpdateExpression="SET #s = :busy, startTime = :t",
            ConditionExpression="attribute_not_exists(#s) OR #s = :idle",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":busy": BotStatus.IN_PROGRESS.value,
                ":idle": BotStatus.IDLE.value,
                ":t": int(time.time()),
            },
        )
        return True
    except Exception:
        return False


def get_bot_status(jurisdiction: str, service_type: str) -> dict | None:
    return aws.get_item(cfg.SETTING_TABLE_NAME, _bot_status_key(jurisdiction, service_type))


def mark_bot_idle_if_overtime(jurisdiction: str, service_type: str,
                              threshold_seconds: int = 60 * 30) -> None:
    rec = get_bot_status(jurisdiction, service_type) or {}
    start = rec.get("startTime")
    if rec.get("status") == BotStatus.IN_PROGRESS.value and start:
        if int(time.time()) - int(start) > threshold_seconds:
            update_bot_filing_status(jurisdiction, service_type, BotStatus.IDLE)


# ---------- Counters ----------
def _counter_key(jurisdiction: str, service_type: str) -> dict:
    return {"pk": f"FILING_COUNT#{jurisdiction}#{service_type}"}


def get_filing_count(jurisdiction: str, service_type: str) -> int:
    item = aws.get_item(cfg.SETTING_TABLE_NAME, _counter_key(jurisdiction, service_type))
    return int(item.get("count", 0)) if item else 0


def alter_filing_count(jurisdiction: str, service_type: str, delta: int) -> int:
    return aws.increment_counter(
        cfg.SETTING_TABLE_NAME, _counter_key(jurisdiction, service_type), "count", delta
    )


# ---------- Settings ----------
def get_setting_from_db(jurisdiction: str, service_type: str) -> dict | None:
    return aws.get_item(cfg.SETTING_TABLE_NAME,
                        {"pk": f"{jurisdiction}#{service_type}"})


# ---------- SSM / credentials ----------
def get_confidential_data(jurisdiction: str, service_type: str) -> dict:
    setting = get_setting_from_db(jurisdiction, service_type) or {}
    out: dict[str, Any] = {}
    if login := setting.get("loginParametersPath"):
        out["credentials"] = json.loads(aws.get_parameter(login))
    if pay := setting.get("paymentParametersPath"):
        out["payment"] = json.loads(aws.get_parameter(pay))
    out["captcha"] = get_captcha_api_key()
    return out


def get_payment_details(path: str) -> dict:
    return json.loads(aws.get_parameter(path))


def get_state_credentials(path: str) -> dict:
    return json.loads(aws.get_parameter(path))


def get_captcha_api_key() -> str:
    return aws.get_parameter("/autograf/captcha/parameters/")


# ---------- Jurisdiction ----------
_JURISDICTION_MAP = {
    "FL": "FLORIDA", "CA": "CALIFORNIA", "NY": "NEW_YORK", "DE": "DELAWARE",
    "TX": "TEXAS", "NV": "NEVADA", "MI": "MICHIGAN", "NC": "NORTH_CAROLINA",
    "RI": "RHODE_ISLAND", "MA": "MASSACHUSETTS", "OK": "OKLAHOMA",
}


def jurisdiction_full_name_mapping(code: str) -> str:
    return _JURISDICTION_MAP.get(code.upper(), code.upper())


def jurisdiction_map(name: str) -> str:
    reverse = {v: k for k, v in _JURISDICTION_MAP.items()}
    return reverse.get(name.upper(), name.upper())


_STATE_INDEPENDENT = {"EIN", "WRITTEN_CONSENT", "CID_PIN", "KIT_DOCUMENTS"}


def is_state_independent_service(service_type: str) -> bool:
    return service_type in _STATE_INDEPENDENT


def remove_trailing_spaces(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: remove_trailing_spaces(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [remove_trailing_spaces(v) for v in obj]
    if isinstance(obj, str):
        return obj.strip()
    return obj


def delay(seconds: float) -> None:
    time.sleep(seconds)


def get_sos_timeout_config(jurisdiction: str, service_type: str) -> dict | None:
    return aws.get_item(cfg.SETTING_TABLE_NAME,
                        {"pk": f"SOS_TIMEOUT_CONFIG#{jurisdiction}#{service_type}"})


def get_cid_from_db(entity_id: str) -> dict | None:
    return aws.get_item(cfg.SETTING_TABLE_NAME, {"pk": f"CID#{entity_id}"})


def json_response(status: int, body: Any, *, extra_headers: dict | None = None) -> dict:
    from .constants import CORS_HEADERS
    headers = {"Content-Type": "application/json", **CORS_HEADERS}
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": status, "headers": headers, "body": json.dumps(body, default=str)}


def load_event_body(event: dict) -> dict:
    raw = event.get("body")
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}
