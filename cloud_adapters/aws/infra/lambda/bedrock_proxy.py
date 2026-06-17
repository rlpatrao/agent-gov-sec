"""
cloud_adapters/aws/infra/lambda/bedrock_proxy.py — API Gateway → Bedrock proxy.

The LLM-egress chokepoint and the out-of-process model-boundary enforcement
point (mechanism 4, full-out-of-process; see docs/governance-authority.md). API
Gateway forwards governed agent requests here; this function resolves the
caller's control posture from the NHI-keyed policy registry and enforces the
**entire model boundary** server-side — input guards, the tool-call plan, and
output guards — before and after calling Bedrock Converse. None of it is driven
by the request body; the policy comes from the deployed registry, and the agent
process cannot alter it.

Enforced here (all from the resolved policy, fail-closed on an unknown identity):
  * Identity — ``x-agent-type`` must resolve to a policy in the registry, else
    403. ``GOV_ALLOWED_NHI`` optionally pins which NHI ids may call at all.
  * Model pinning — model id injected from ``BEDROCK_MODEL_ID``; a body
    ``modelId`` is ignored and logged.
  * Input guards — prompt-injection, credential (deny or in-place redact), and
    context-budget over the request messages + system.
  * Tool-call plan — the model's ``toolUse`` blocks are checked against the
    agent's capability allow/deny-list and a blocked-pattern scan of the args; a
    disallowed plan is blocked before it returns to the agent.
  * Output guards — PII/credential redaction and blocked-pattern scan over the
    response text, plus a redaction backstop over inbound ``toolResult`` blocks.

This is the authoritative fail-closed gate. The richer in-process ``agent_os``
detectors remain as defense-in-depth.

Deployment: bundle ``governance/enforcement_core.py`` and
``governance/policy_registry.py`` (both dependency-free at import) into the
Lambda package, and supply the registry via ``GOV_POLICY_REGISTRY`` (JSON) or
``GOV_POLICY_REGISTRY_PATH`` (file). Build the registry with
``governance.policy_registry.export_registry_json``. Pure boto3 + stdlib
otherwise.
"""

import json
import os

from governance import enforcement_core as ec
from governance.policy_registry import load_registry, policy_for

_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-1")
_ALLOWED = ("messages", "system", "toolConfig", "inferenceConfig", "additionalModelRequestFields")

_client = None
_registry_cache = None


def _bedrock_client():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("bedrock-runtime", region_name=_REGION)
    return _client


def _log(event, **fields):
    print(json.dumps({"event": event, **fields}))


# ── Policy resolution (fail-closed) ───────────────────────────────────────────

def _registry():
    global _registry_cache
    if _registry_cache is None:
        raw = os.environ.get("GOV_POLICY_REGISTRY")
        if not raw:
            path = os.environ.get("GOV_POLICY_REGISTRY_PATH")
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
        _registry_cache = load_registry(raw) if raw else {}
    return _registry_cache


def _allowed_nhi():
    raw = os.environ.get("GOV_ALLOWED_NHI", "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()} if raw else None


def resolve(headers):
    """Return (policy_dict | None, agent_type, nhi_id). Lookup is case-insensitive."""
    lowered = {(k or "").lower(): v for k, v in (headers or {}).items()}
    agent_type = lowered.get("x-agent-type")
    nhi_id = lowered.get("x-nhi-id")
    allowed = _allowed_nhi()
    if allowed is not None and nhi_id not in allowed:
        _log("proxy.nhi_denied", nhi=nhi_id, agent=agent_type)
        return None, agent_type, nhi_id
    return policy_for(_registry(), agent_type), agent_type, nhi_id


# ── Converse-shape text helpers ────────────────────────────────────────────

def _iter_text_blocks(messages):
    """Yield (block, key) for every text/toolResult-text block in messages."""
    for m in messages or []:
        for block in (m.get("content") or []):
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    yield block, "text"
                tr = block.get("toolResult")
                if isinstance(tr, dict):
                    for inner in (tr.get("content") or []):
                        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                            yield inner, "text"


def _input_text(body):
    parts = []
    for blk in (body.get("system") or []):
        if isinstance(blk, dict) and isinstance(blk.get("text"), str):
            parts.append(blk["text"])
    for blk, _ in _iter_text_blocks(body.get("messages")):
        parts.append(blk["text"])
    return "\n".join(parts)


def _resp(status, payload):
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(payload)}


def handler(event, context):
    policy, agent_type, nhi_id = resolve(event.get("headers") or {})
    if policy is None:
        # Fail-closed: unknown identity / no registry entry.
        _log("proxy.policy_denied", agent=agent_type, nhi=nhi_id)
        return _resp(403, {"error": "no_governance_policy", "agent_type": agent_type})

    posture = ec.ModelBoundaryPosture.from_dict(policy.get("model_boundary") or {})

    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return _resp(400, {"error": "invalid JSON body"})
    if not body.get("messages"):
        return _resp(400, {"error": "missing 'messages'"})

    if "modelId" in body and body["modelId"] != _MODEL_ID:
        _log("proxy.modelid_override_ignored", agent=agent_type, requested=body.get("modelId"))

    # ── Input guards (over messages + system) ──
    verdict = ec.enforce_input(_input_text(body), posture)
    if verdict.blocked:
        _log("proxy.input_blocked", agent=agent_type, code=verdict.code, reason=verdict.reason)
        return _resp(403, {"error": verdict.code, "reason": verdict.reason})
    # Credential redaction mutates text in place across messages + system.
    if posture.credential_enabled and posture.credential_mode == "redact":
        red = _redact_messages(body)
        if red:
            _log("proxy.input_redacted", agent=agent_type, spans=red)

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

    # ── Tool-call plan: block a disallowed tool the model intends to call ──
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("toolUse"), dict):
            tu = block["toolUse"]
            r = ec.check_tool_plan(
                tu.get("name", ""), tu.get("input", {}),
                allowed=list(policy.get("allowed_tools") or []),
                denied=list(policy.get("denied_tools") or []),
                blocked_patterns=list(posture.blocked_patterns),
            )
            if r.blocked:
                _log("proxy.tool_plan_blocked", agent=agent_type, tool=tu.get("name"), code=r.code)
                return _resp(403, {"error": r.code, "reason": r.reason, "tool": tu.get("name")})

    # ── Output guards: redact text blocks; block on output-side patterns ──
    out_redactions = 0
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            ov = ec.enforce_output(block["text"], posture)
            if ov.blocked:
                _log("proxy.output_blocked", agent=agent_type, code=ov.code)
                return _resp(403, {"error": ov.code, "reason": ov.reason})
            block["text"] = ov.text
            out_redactions += ov.redactions
    if out_redactions:
        _log("proxy.output_redacted", agent=agent_type, spans=out_redactions)

    return _resp(200, {
        "output": output,
        "stopReason": out.get("stopReason"),
        "usage": out.get("usage", {}),
    })


def _redact_messages(body):
    """Redact credentials in every text block of messages + system, in place.
    Returns the total spans redacted."""
    total = 0
    for blk in (body.get("system") or []):
        if isinstance(blk, dict) and isinstance(blk.get("text"), str):
            blk["text"], n = ec.redact_credentials(blk["text"])
            total += n
    for blk, _ in _iter_text_blocks(body.get("messages")):
        blk["text"], n = ec.redact_credentials(blk["text"])
        total += n
    return total
