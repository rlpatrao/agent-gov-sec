"""
cloud_adapters/aws/agentcore/response_interceptor.py — AgentCore Gateway response interceptor.

Translates an AgentCore Gateway *response* interceptor event into
`governance.remote.enforce` output-side calls. Handles two response-side controls:
PII/credential redaction over response text (and tool results flowing back), and
tool-list filtering for tool-discovery results (drop tools the agent's policy does
not permit). Delegates to the shared enforcement library.

Event contract (relevant subset):
    {
      "requestContext": {"agentType": "FinOps", "nhiId": "..."},
      "response": {"text": "..."} | {"toolList": ["a", "b"]} | {"toolResult": {"text": "..."}},
    }
Returns the transformed response (redacted text / filtered tool list), or a deny.
"""

import json
import os

from governance.remote import enforce
from governance.shared.policy_registry import load_registry, policy_for

_registry_cache = None


def _log(event, **fields):
    print(json.dumps({"event": event, **fields}))


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


def handle(event):
    """Pure handler (testable offline)."""
    ctx = event.get("requestContext") or {}
    agent_type = ctx.get("agentType")
    registry = _registry()
    session = enforce.session_for(agent_type, registry, nhi_id=ctx.get("nhiId"))
    if session is None:
        _log("agentcore.response_denied", agent=agent_type, code="no_governance_policy")
        return {"decision": "deny", "code": "no_governance_policy"}

    resp = dict(event.get("response") or {})

    # Tool-discovery filtering: keep only tools the agent's policy permits.
    if "toolList" in resp:
        allowed = set((policy_for(registry, agent_type) or {}).get("allowed_tools") or [])
        kept = [t for t in resp["toolList"] if t in allowed]
        if len(kept) != len(resp["toolList"]):
            _log("agentcore.toollist_filtered", agent=agent_type,
                 dropped=[t for t in resp["toolList"] if t not in allowed])
        resp["toolList"] = kept
        return {"decision": "allow", "response": resp}

    # Output / tool-result redaction.
    for key in ("text",):
        if isinstance(resp.get(key), str):
            v = enforce.enforce_output(session, resp[key])
            if v.blocked:
                _log("agentcore.response_blocked", agent=agent_type, code=v.code)
                return {"decision": "deny", "code": v.code, "reason": v.reason}
            resp[key] = v.text
    tr = resp.get("toolResult")
    if isinstance(tr, dict) and isinstance(tr.get("text"), str):
        tr["text"] = enforce.enforce_output(session, tr["text"]).text

    return {"decision": "allow", "response": resp}


def handler(event, context):
    return handle(event)
