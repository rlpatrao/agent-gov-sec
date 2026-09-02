"""
cloud_adapters/azure/infra/functions/llm_proxy.py — APIM → Azure OpenAI proxy.

The LLM-egress chokepoint for the Azure "Method 1" deployment — the counterpart
of ``cloud_adapters/aws/infra/lambda/bedrock_proxy.py``. A thin Azure-transport
adapter: it translates the chat-completions request/response to/from
``galaxy_gov.remote.enforce``, which resolves the caller's policy from the
NHI-keyed registry (fail-closed) and re-verifies it with the *same* enforcement
library the in-process pipeline uses (trust-but-verify). No control logic lives
here.

Enforced (all from the resolved policy, never the request body):
  * Identity — ``x-agent-type`` must resolve to a registry policy, else 403.
  * Model pinning — deployment from ``AZURE_OPENAI_DEPLOYMENT``; a body ``model``
    is ignored.
  * Input guards — prompt-injection, credential (redact/deny), context-budget.
  * Tool-call plan — capability allow-list + blocked-pattern over ``tool_calls``.
  * Output guards — PII/credential redaction + content-safety over the response.

``enforce_llm`` is the pure decision function (transport-agnostic, unit-testable);
``function_app.py`` adapts an Azure Functions ``HttpRequest`` onto it. The Azure
OpenAI call is isolated in ``_call_aoai`` (monkeypatchable in tests / replaceable
with the ``openai`` SDK) and uses only the stdlib so the package imports without
extra deps.
"""

from __future__ import annotations

import json
import os
import urllib.request

from galaxy_gov.remote import enforce
from galaxy_gov.shared.policy_registry import load_registry

_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")
_ALLOWED = ("messages", "tools", "tool_choice", "temperature", "max_tokens", "response_format")

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


def _content_text(content) -> str:
    """Azure OpenAI content is either a string or a list of {type,text} parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return ""


def _input_text(body) -> str:
    return "\n".join(_content_text(m.get("content")) for m in (body.get("messages") or []) if isinstance(m, dict))


def _call_aoai(body: dict) -> dict:
    """POST to Azure OpenAI chat-completions. The AOAI key is injected here at the
    edge (Key-Vault-backed named value in APIM, or the proxy's Key Vault fetch) so
    it never reaches the caller. Isolated for testability."""
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    url = f"{endpoint}/openai/deployments/{_DEPLOYMENT}/chat/completions?api-version={_API_VERSION}"
    from cloud_adapters.azure.secrets import TokenProvider
    api_key = TokenProvider(secret_name="azure-openai-key", env_var_fallback="AZURE_OPENAI_KEY").get_api_key()
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "api-key": api_key}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # pragma: no cover - needs live AOAI
        return json.loads(resp.read().decode("utf-8"))


def _redact_output(node, session):
    """Recursively run output guards over every string the model emitted — not
    just message content. Returns (redacted_node, blocked_verdict_or_None)."""
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


def enforce_llm(body: dict, headers: dict) -> tuple[int, dict]:
    """Pure decision function. Returns (status_code, payload)."""
    headers = {(k or "").lower(): v for k, v in (headers or {}).items()}
    agent_type = headers.get("x-agent-type")
    session = enforce.session_for(agent_type, _registry(), nhi_id=headers.get("x-nhi-id"))
    if session is None:
        _log("llm_proxy.policy_denied", agent=agent_type)
        return 403, {"error": "no_governance_policy", "agent_type": agent_type}

    if not body.get("messages"):
        return 400, {"error": "missing 'messages'"}
    if "model" in body and body["model"] != _DEPLOYMENT:
        _log("llm_proxy.model_override_ignored", agent=agent_type, requested=body.get("model"))

    v = enforce.enforce_input(session, _input_text(body))
    if v.blocked:
        _log("llm_proxy.input_blocked", agent=agent_type, code=v.code, reason=v.reason)
        return 403, {"error": v.code, "reason": v.reason}

    payload = {"model": _DEPLOYMENT}
    for k in _ALLOWED:
        if k in body and body[k] not in (None, [], {}):
            payload[k] = body[k]
    try:
        out = _call_aoai(payload)
    except Exception as e:  # pragma: no cover - needs live AOAI
        return 502, {"error": f"{type(e).__name__}: {str(e)[:400]}"}

    # Tool-call plan: block a disallowed / blocked-pattern tool call before it returns.
    tool_calls = []
    for choice in out.get("choices", []):
        for tc in ((choice.get("message") or {}).get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = {"_raw": args}
            tool_calls.append((fn.get("name", ""), args or {}))
    tv = enforce.enforce_tool_plan(session, tool_calls)
    if tv.blocked:
        _log("llm_proxy.tool_plan_blocked", agent=agent_type, code=tv.code)
        return 403, {"error": tv.code, "reason": tv.reason}

    redacted, ov = _redact_output(out.get("choices", []), session)
    if ov is not None:
        _log("llm_proxy.output_blocked", agent=agent_type, code=ov.code)
        return 403, {"error": ov.code, "reason": ov.reason}

    return 200, {"choices": redacted, "usage": out.get("usage", {}), "model": _DEPLOYMENT}
