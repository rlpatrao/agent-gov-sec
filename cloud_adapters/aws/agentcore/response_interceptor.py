"""
cloud_adapters/aws/agentcore/response_interceptor.py — AgentCore Gateway RESPONSE interceptor.

Redacts PII/credentials from outbound MCP responses (tool results flowing back to
the agent), using the same `governance` enforcement library as the in-process
tier. Identity-independent content safety; per-agent authz is AgentCore Policy's.

AgentCore interceptor contract:
  input :  event["mcp"]["gatewayResponse"] = {"body": ..., "statusCode": ...}
  output:  {"interceptorOutputVersion":"1.0","mcp":{"transformedGatewayResponse":{"body":..,"statusCode":..}}}
"""

import json

from governance.shared.enforcement.session import build_enforcement

_POSTURE = {"model_boundary": {
    "injection_enabled": False, "credential_enabled": False, "budget_enabled": False,
    "output_pii_enabled": True, "blocked_patterns": [],
}}
_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_enforcement(_POSTURE, agent_id="gateway", agent_type="gateway")
    return _session


def handler(event, context):
    gw_resp = (event.get("mcp", {}) or {}).get("gatewayResponse", {}) or {}
    body = gw_resp.get("body", {})
    status = gw_resp.get("statusCode", 200)

    text = body if isinstance(body, str) else json.dumps(body)
    v = _sess().check_output(text)
    if v.text != text:
        print(json.dumps({"event": "interceptor.response_redacted"}))
    try:
        new_body = json.loads(v.text)
    except (ValueError, TypeError):
        new_body = v.text

    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {"body": new_body, "statusCode": status}}}
