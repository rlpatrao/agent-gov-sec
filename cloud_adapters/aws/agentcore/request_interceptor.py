"""
cloud_adapters/aws/agentcore/request_interceptor.py — AgentCore Gateway REQUEST interceptor.

Runs the framework's content controls on inbound MCP requests, over the same
`governance` enforcement library used in-process (trust-but-verify). Division of
labor: AgentCore Policy (Cedar) does per-agent tool authorization; this
interceptor does the identity-independent content safety Cedar cannot express —
prompt-injection detection and credential blocking on tool-call arguments.

AgentCore interceptor contract (gateway-interceptors-examples):
  input :  event["mcp"]["gatewayRequest"]["body"]  = the MCP JSON-RPC request
  output:  {"interceptorOutputVersion":"1.0","mcp":{"transformedGatewayRequest":{"body": <body>}}}
           to allow; include "transformedGatewayResponse" instead to short-circuit
           (deny) — the gateway returns that immediately.
"""

import json

from governance.shared.enforcement.session import build_enforcement

# Identity-independent content posture (Cedar owns per-agent tool authz). Injection
# blocks at medium+, credentials are denied outright, budget is irrelevant for a
# single tool call. The blocked-pattern set guards tool arguments.
_POSTURE = {"model_boundary": {
    "injection_enabled": True, "injection_threshold": "medium",
    "credential_enabled": True, "credential_mode": "deny",
    "budget_enabled": False, "output_pii_enabled": True,
    "blocked_patterns": ["DROP TABLE", "DELETE FROM", "rm -rf"],
}}
_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_enforcement(_POSTURE, agent_id="gateway", agent_type="gateway")
    return _session


def _scannable_text(body):
    """Text to content-scan from an MCP JSON-RPC request: tool name + arguments."""
    params = (body or {}).get("params") or {}
    parts = []
    name = params.get("name")
    if isinstance(name, str):
        parts.append(name)
    args = params.get("arguments")
    if isinstance(args, dict):
        parts.append(json.dumps(args))
    elif isinstance(args, str):
        parts.append(args)
    return "\n".join(parts)


def _deny(body, code, reason):
    """Short-circuit the gateway with an MCP error (the deny envelope)."""
    err = {"jsonrpc": "2.0", "id": (body or {}).get("id"),
           "error": {"code": -32600, "message": f"governance blocked: {code} — {reason}"}}
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {"body": err, "statusCode": 200}}}


def handler(event, context):
    body = (event.get("mcp", {}) or {}).get("gatewayRequest", {}).get("body", {}) or {}
    try:
        text = _scannable_text(body)
        if text:
            v = _sess().check_input(text)
            if v.blocked:
                print(json.dumps({"event": "interceptor.request_blocked",
                                  "code": v.code, "method": body.get("method")}))
                return _deny(body, v.code, v.reason)
        return {"interceptorOutputVersion": "1.0",
                "mcp": {"transformedGatewayRequest": {"body": body}}}
    except Exception as e:
        # Fail closed: any internal guard error denies the request rather than
        # letting an un-inspected call through the gateway.
        print(json.dumps({"event": "interceptor.request_error_failclosed",
                          "error": f"{type(e).__name__}: {str(e)[:200]}",
                          "method": (body or {}).get("method")}))
        return _deny(body, "interceptor_error",
                     "request interceptor failed; denied (fail-closed)")
