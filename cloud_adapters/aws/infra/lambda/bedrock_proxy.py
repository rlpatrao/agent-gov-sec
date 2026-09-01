"""
cloud_adapters/aws/infra/lambda/bedrock_proxy.py — API Gateway → Bedrock proxy.

The LLM-egress chokepoint. A thin AWS-transport adapter: it translates the
Converse request/response to/from `governance.remote.enforce`, which resolves the
caller's policy from the NHI-keyed registry (fail-closed) and re-verifies it with
the *same* enforcement library the in-process pipeline uses (trust-but-verify).
No control logic lives here.

Enforced (all from the resolved policy, never the request body):
  * Identity — `x-agent-type` must resolve to a registry policy, else 403.
  * Model pinning — model id from `BEDROCK_MODEL_ID`; a body `modelId` is ignored.
  * Input guards — prompt-injection, credential (redact/deny), context-budget.
  * Tool-call plan — capability allow-list + blocked-pattern over `toolUse`.
  * Output guards — PII/credential redaction + content-safety over the response.

Deployment: bundle `governance/{shared,remote}` (and the toolkit) into the image;
supply the registry via `GOV_POLICY_REGISTRY` (JSON), `GOV_POLICY_REGISTRY_URI`
(the centralized store, `s3://bucket/key`), or `GOV_POLICY_REGISTRY_PATH`.
"""

import json
import os

from governance.remote import enforce
from governance.shared.policy_registry import RegistryUnavailable, resolve_registry

_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-1")
_ALLOWED = ("messages", "system", "toolConfig", "inferenceConfig", "additionalModelRequestFields")

_client = None


def _bedrock_client():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("bedrock-runtime", region_name=_REGION)
    return _client


def _log(event, **fields):
    print(json.dumps({"event": event, **fields}))


def _registry():
    """Resolve the registry through the centralized-store contract (inline JSON >
    GOV_POLICY_REGISTRY_URI > baked file, TTL-cached). An unresolvable registry
    yields an empty document, which denies every request at policy_for."""
    try:
        return resolve_registry()
    except RegistryUnavailable:
        return {}


def _resp(status, payload):
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(payload)}


def _iter_text_blocks(messages):
    for m in messages or []:
        for block in (m.get("content") or []):
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    yield block
                tr = block.get("toolResult")
                if isinstance(tr, dict):
                    for inner in (tr.get("content") or []):
                        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                            yield inner


def _input_text(body):
    parts = [b["text"] for b in (body.get("system") or []) if isinstance(b, dict) and isinstance(b.get("text"), str)]
    parts += [b["text"] for b in _iter_text_blocks(body.get("messages"))]
    return "\n".join(parts)


def _redact_output(node, session):
    """Recursively run output guards over every string the model emitted — not
    just top-level text blocks. Covers toolUse.input, reasoningContent, nested
    toolResult/json content. Returns (redacted_node, blocked_verdict_or_None)."""
    if isinstance(node, str):
        ov = enforce.enforce_output(session, node)
        return (None, ov) if ov.blocked else (ov.text, None)
    if isinstance(node, dict):
        out = {}
        for k, val in node.items():
            new, blk = _redact_output(val, session)
            if blk is not None:
                return None, blk
            out[k] = new
        return out, None
    if isinstance(node, list):
        out = []
        for item in node:
            new, blk = _redact_output(item, session)
            if blk is not None:
                return None, blk
            out.append(new)
        return out, None
    return node, None


def handler(event, context):
    headers = {(k or "").lower(): v for k, v in (event.get("headers") or {}).items()}
    agent_type = headers.get("x-agent-type")
    session = enforce.session_for(agent_type, _registry(), nhi_id=headers.get("x-nhi-id"))
    if session is None:
        _log("proxy.policy_denied", agent=agent_type)
        return _resp(403, {"error": "no_governance_policy", "agent_type": agent_type})

    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return _resp(400, {"error": "invalid JSON body"})
    if not body.get("messages"):
        return _resp(400, {"error": "missing 'messages'"})
    if "modelId" in body and body["modelId"] != _MODEL_ID:
        _log("proxy.modelid_override_ignored", agent=agent_type, requested=body.get("modelId"))

    # Input guards (injection / credential / budget) over messages + system.
    v = enforce.enforce_input(session, _input_text(body))
    if v.blocked:
        _log("proxy.input_blocked", agent=agent_type, code=v.code, reason=v.reason)
        return _resp(403, {"error": v.code, "reason": v.reason})

    kwargs = {"modelId": _MODEL_ID}
    for k in _ALLOWED:
        if k in body and body[k] not in (None, [], {}):
            kwargs[k] = body[k]
    try:
        out = _bedrock_client().converse(**kwargs)
    except Exception as e:
        return _resp(502, {"error": f"{type(e).__name__}: {str(e)[:400]}"})

    output = out.get("output", {})
    content = (output.get("message", {}) or {}).get("content", []) or []

    # Tool-call plan: block a disallowed / blocked-pattern toolUse before it returns.
    tool_calls = [(b["toolUse"].get("name", ""), b["toolUse"].get("input", {}))
                  for b in content if isinstance(b, dict) and isinstance(b.get("toolUse"), dict)]
    tv = enforce.enforce_tool_plan(session, tool_calls)
    if tv.blocked:
        _log("proxy.tool_plan_blocked", agent=agent_type, code=tv.code)
        return _resp(403, {"error": tv.code, "reason": tv.reason})

    # Output guards: redact / block over ALL model-emitted content (text,
    # toolUse.input, reasoning, nested blocks) — not just top-level text.
    output, ov = _redact_output(output, session)
    if ov is not None:
        _log("proxy.output_blocked", agent=agent_type, code=ov.code)
        return _resp(403, {"error": ov.code, "reason": ov.reason})

    return _resp(200, {"output": output, "stopReason": out.get("stopReason"), "usage": out.get("usage", {})})
