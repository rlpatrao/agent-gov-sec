"""
cloud_adapters/aws/infra/lambda/a2a_broker.py — the agent-to-agent chokepoint.

A2A authorization enforced out-of-process (mechanism 4, full-out-of-process; see
docs/governance-authority.md). Agent-to-agent dispatch never traverses the LLM
proxy, so moving recipient authorization out of the agent requires its own
broker. Before a sender dispatches to a recipient, the call is authorized here
against the sender's allow-list resolved from the NHI-keyed policy registry —
not from a list the sender passes in.

Fail-closed: a sender absent from the registry is denied; a recipient not on the
sender's resolved ``allowed_recipients`` is denied. Dependency-free (registry +
stdlib), so it bundles into a Lambda with no agent-codebase dependency.

``authorize_dispatch`` is the pure decision function; the in-process dispatcher
(``a2a.dispatcher``) calls it directly when ``GOV_A2A_BROKER_ENDPOINT`` is set,
and the Lambda ``handler`` exposes it over HTTP.
"""

import json
import os

from governance.policy_registry import authorize_recipient, load_registry

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


def authorize_dispatch(sender_type, recipient, registry=None):
    """Return (allowed, reason). Thin wrapper over the shared registry decision
    so the broker and the in-process dispatcher apply identical authorization."""
    return authorize_recipient(sender_type, recipient,
                               registry if registry is not None else _registry())


def _resp(status, payload):
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(payload)}


def handler(event, context):
    headers = {(k or "").lower(): v for k, v in (event.get("headers") or {}).items()}
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return _resp(400, {"error": "invalid JSON body"})

    sender_type = headers.get("x-agent-type") or body.get("sender_type")
    recipient = body.get("recipient")
    if not sender_type or not recipient:
        return _resp(400, {"error": "missing sender_type/recipient"})

    allowed, reason = authorize_dispatch(sender_type, recipient)
    _log("a2a_broker.decision", sender=sender_type, recipient=recipient,
         decision="allow" if allowed else "deny", reason=reason)
    if not allowed:
        return _resp(403, {"error": "recipient_not_allowed", "reason": reason})
    return _resp(200, {"decision": "allow"})
