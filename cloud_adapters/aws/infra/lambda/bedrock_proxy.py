"""
cloud_adapters/aws/infra/lambda/bedrock_proxy.py — API Gateway → Bedrock proxy (WS5).

The server side of the ``apigw-bedrock`` egress chokepoint. API Gateway (REST,
Lambda-proxy integration, x-api-key usage plan) forwards governed agent requests
here; this function calls **Amazon Bedrock Converse** with the configured model
and returns the Converse JSON. Bedrock credentials (the Lambda execution role's
``bedrock:InvokeModel``) never leave AWS — the agent only ever holds the API key.

Request body (JSON) = Converse arguments minus ``modelId``:
    {"messages": [...], "system": [...], "toolConfig": {...}, "inferenceConfig": {...}}
The model id is injected from the ``BEDROCK_MODEL_ID`` env var (an inference
profile id such as ``us.anthropic.claude-sonnet-4-6``), so callers
can't pick an arbitrary model.

Response body (JSON):
    {"output": {...}, "stopReason": "...", "usage": {...}}

Pure boto3 — no extra layers. Pairs with the client-side
``agent_framework_adapters/langgraph/bedrock_gateway.BedrockGatewayChatModel``.
"""

import json
import os

import boto3

_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-1")

# Reused across warm invocations.
_client = boto3.client("bedrock-runtime", region_name=_REGION)

# Converse args we accept from the caller; modelId is fixed server-side.
_ALLOWED = ("messages", "system", "toolConfig", "inferenceConfig", "additionalModelRequestFields")


def _resp(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return _resp(400, {"error": "invalid JSON body"})

    if not body.get("messages"):
        return _resp(400, {"error": "missing 'messages'"})

    kwargs = {"modelId": _MODEL_ID}
    for k in _ALLOWED:
        if k in body and body[k] not in (None, [], {}):
            kwargs[k] = body[k]

    try:
        out = _client.converse(**kwargs)
    except Exception as e:  # ClientError, validation, throttling, …
        # Surface the Bedrock error so the client narrates it instead of a 502.
        return _resp(502, {"error": f"{type(e).__name__}: {str(e)[:400]}"})

    return _resp(200, {
        "output": out.get("output", {}),
        "stopReason": out.get("stopReason"),
        "usage": out.get("usage", {}),
    })
