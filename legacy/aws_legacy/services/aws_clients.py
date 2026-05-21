"""AWS SDK (boto3) wrappers — port of `src/awsServices/*`.

Thin, stateless functions used directly from Lambda handlers.
"""
from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.config import Config

from . import config as cfg

_boto_cfg = Config(region_name=cfg.AWS_REGION, retries={"max_attempts": 3, "mode": "standard"})

_dynamodb = boto3.resource("dynamodb", config=_boto_cfg)
_sqs = boto3.client("sqs", config=_boto_cfg)
_s3 = boto3.client("s3", config=_boto_cfg)
_events = boto3.client("events", config=_boto_cfg)
_ssm = boto3.client("ssm", config=_boto_cfg)


# ---------- DynamoDB ----------
def get_item(table: str, key: dict) -> dict | None:
    return _dynamodb.Table(table).get_item(Key=key).get("Item")


def put_item(table: str, item: dict) -> None:
    _dynamodb.Table(table).put_item(Item=item)


def update_item(table: str, key: dict, **kwargs) -> dict:
    return _dynamodb.Table(table).update_item(Key=key, **kwargs)


def query_items(table: str, **kwargs) -> list[dict]:
    return _dynamodb.Table(table).query(**kwargs).get("Items", [])


def scan_items(table: str, **kwargs) -> list[dict]:
    return _dynamodb.Table(table).scan(**kwargs).get("Items", [])


def delete_item(table: str, key: dict) -> None:
    _dynamodb.Table(table).delete_item(Key=key)


def increment_counter(table: str, key: dict, attr: str, delta: int = 1) -> int:
    resp = _dynamodb.Table(table).update_item(
        Key=key,
        UpdateExpression=f"ADD #c :d",
        ExpressionAttributeNames={"#c": attr},
        ExpressionAttributeValues={":d": delta},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"][attr])


def transact_write(items: list[dict]) -> None:
    boto3.client("dynamodb", config=_boto_cfg).transact_write_items(TransactItems=items)


# ---------- S3 ----------
def s3_get_object(bucket: str, key: str) -> bytes:
    return _s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def s3_put_object(bucket: str, key: str, body: bytes, content_type: str | None = None) -> None:
    extra = {"ContentType": content_type} if content_type else {}
    _s3.put_object(Bucket=bucket, Key=key, Body=body, **extra)


def s3_presigned_url(bucket: str, key: str, expires_in: int = 7200) -> str:
    return _s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
    )


# ---------- SQS ----------
def sqs_send_message(queue_url: str, body: dict, group_id: str | None = None,
                     dedup_id: str | None = None) -> dict:
    kwargs: dict[str, Any] = {"QueueUrl": queue_url, "MessageBody": json.dumps(body)}
    if queue_url.endswith(".fifo"):
        if group_id is None:
            raise ValueError("FIFO queue requires MessageGroupId")
        kwargs["MessageGroupId"] = group_id
        if dedup_id:
            kwargs["MessageDeduplicationId"] = dedup_id
    return _sqs.send_message(**kwargs)


def sqs_delete_message(queue_url: str, receipt_handle: str) -> None:
    _sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)


def sqs_receive_messages(queue_url: str, max_messages: int = 1, wait_time: int = 0) -> list[dict]:
    return _sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=max_messages, WaitTimeSeconds=wait_time
    ).get("Messages", [])


# ---------- EventBridge ----------
def put_event(source: str, destination: str, detail: dict,
              detail_type: str = "autografPipeline",
              bus_name: str | None = None) -> dict:
    payload = {**detail, "source": source, "destination": destination}
    return _events.put_events(
        Entries=[
            {
                "Source": source,
                "DetailType": detail_type,
                "Detail": json.dumps(payload),
                "EventBusName": bus_name or cfg.EVENT_BUS_NAME,
            }
        ]
    )


# ---------- SSM ----------
def get_parameter(name: str, decrypt: bool = True) -> str:
    return _ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
