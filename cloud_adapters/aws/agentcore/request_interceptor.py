"""
cloud_adapters/aws/agentcore/request_interceptor.py — AgentCore Gateway request interceptor.

A thin transport adapter: it translates an AgentCore Gateway *request* interceptor
event into `governance.remote.enforce` calls and back. AgentCore Policy (Cedar)
handles the coarse allow/deny; this interceptor adds the content controls Cedar
cannot express — prompt-injection, credential handling, and context-budget on LLM
requests, and the blocked-pattern scan on tool-call arguments — re-verifying the
same governance library the in-process pipeline runs (trust-but-verify).

Event contract (the relevant subset of the AgentCore interceptor payload):
    {
      "requestContext": {"agentType": "FinOps", "nhiId": "..."},
      "target": "model" | "tool" | "agent",
      "input":  {"messages": [...], "system": [...]},   # for target == "model"
      "tool":   {"name": "...", "arguments": {...}},     # for target == "tool"
    }
Returns ``{"decision": "allow"|"deny", "reason": ..., "input"/"tool": <transformed>}``.
The registry is supplied via GOV_POLICY_REGISTRY / GOV_POLICY_REGISTRY_PATH and is
shared with the LLM/data/A2A chokepoints.
"""

import json
import os

from governance.remote import enforce
from governance.shared.policy_registry import load_registry

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


def _input_text(inp):
    parts = [b.get("text", "") for b in (inp.get("system") or []) if isinstance(b, dict)]
    for m in inp.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [b.get("text", "") for b in c if isinstance(b, dict)]
    return "\n".join(p for p in parts if p)


def _deny(agent_type, code, reason):
    _log("agentcore.request_denied", agent=agent_type, code=code)
    return {"decision": "deny", "code": code, "reason": reason}


def handle(event):
    """Pure handler (testable offline). The Lambda `handler` wraps it."""
    ctx = event.get("requestContext") or {}
    agent_type = ctx.get("agentType")
    session = enforce.session_for(agent_type, _registry(), nhi_id=ctx.get("nhiId"))
    if session is None:
        return _deny(agent_type, "no_governance_policy", f"no policy for {agent_type!r}")

    target = event.get("target") or ("tool" if event.get("tool") else "model")

    if target in ("model", "agent"):
        v = enforce.enforce_input(session, _input_text(event.get("input") or {}))
        if v.blocked:
            return _deny(agent_type, v.code, v.reason)
        # Return the (possibly credential-redacted) input.
        out = dict(event.get("input") or {})
        return {"decision": "allow", "input": out, "redacted_text": v.text}

    if target == "tool":
        tool = event.get("tool") or {}
        v = enforce.enforce_tool_plan(session, [(tool.get("name", ""), tool.get("arguments", {}))])
        if v.blocked:
            return _deny(agent_type, v.code, v.reason)
        return {"decision": "allow", "tool": tool}

    return {"decision": "allow"}


def handler(event, context):
    return handle(event)
