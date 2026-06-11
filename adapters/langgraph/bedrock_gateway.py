"""
adapters.langgraph.bedrock_gateway — client-side model for the apigw-bedrock path.

``BedrockGatewayChatModel`` is a LangChain ``BaseChatModel`` that talks to Amazon
Bedrock **through the API Gateway egress chokepoint** rather than to
``bedrock-runtime`` directly. It converts LangChain messages + bound tools into a
Bedrock **Converse** request, POSTs it (with the ``x-api-key`` +
``x-agent-type`` / ``x-nhi-id`` attribution headers) to
``AWS_BEDROCK_GATEWAY_ENDPOINT``, and parses the Converse response back into an
``AIMessage`` (text + ``tool_calls``).

The server side is ``adapters/aws/infra/lambda/bedrock_proxy.py`` (API Gateway →
Lambda → Bedrock Converse). Bedrock credentials never reach the agent — only the
gateway API key does. This is why the demo can't use langchain-aws's
``ChatBedrockConverse`` here (that calls bedrock-runtime directly over SigV4); the
governed path requires this thin custom model.

Only the Converse subset the three demo agents need is mapped: system prompt,
user/assistant text, ``toolUse``/``toolResult`` blocks, and a tool config built
from the bound LangChain tools.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, List, Optional, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

logger = logging.getLogger(__name__)


class BedrockGatewayChatModel(BaseChatModel):
    """Chat model that reaches Bedrock Converse via the API Gateway chokepoint."""

    endpoint: str
    api_key: str
    model_id: str = "us.anthropic.claude-sonnet-4-6"
    max_tokens: int = 1024
    temperature: float = 0.0
    default_headers: dict = {}
    timeout: float = 60.0
    tool_config: Optional[dict] = None  # Converse toolConfig, set by bind_tools

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "galaxy-bedrock-gateway"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "BedrockGatewayChatModel":
        """Capture the bound tools as a Converse ``toolConfig`` and return a copy."""
        specs = []
        for t in tools:
            fn = convert_to_openai_tool(t)["function"]
            specs.append({
                "toolSpec": {
                    "name": fn["name"],
                    "description": fn.get("description", "") or fn["name"],
                    "inputSchema": {"json": fn.get("parameters", {"type": "object", "properties": {}})},
                }
            })
        return self.model_copy(update={"tool_config": {"tools": specs} if specs else None})

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        system, converse_messages = _to_converse(messages)
        body: dict[str, Any] = {
            "messages": converse_messages,
            "inferenceConfig": {"maxTokens": self.max_tokens, "temperature": self.temperature},
        }
        if system:
            body["system"] = system
        if self.tool_config:
            body["toolConfig"] = self.tool_config

        resp = self._post(body)
        return _from_converse(resp)

    def _post(self, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json", "x-api-key": self.api_key, **(self.default_headers or {})}
        req = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (configured endpoint)
            payload = json.loads(r.read().decode("utf-8"))
        if "error" in payload:
            raise RuntimeError(f"bedrock-gateway: {payload['error']}")
        return payload


# ── LangChain ⇄ Bedrock Converse mapping ──────────────────────────────────────

def _to_converse(messages: List[BaseMessage]) -> tuple[list, list]:
    """Return ``(system_blocks, converse_messages)``. Consecutive same-role turns
    are merged — Bedrock requires strictly alternating user/assistant roles."""
    system: list[dict] = []
    out: list[dict] = []

    def _append(role: str, blocks: list):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        cls = m.__class__.__name__
        if cls == "SystemMessage":
            txt = _text(m.content)
            if txt:
                system.append({"text": txt})
        elif cls == "HumanMessage":
            _append("user", [{"text": _text(m.content)}])
        elif cls == "AIMessage":
            blocks: list[dict] = []
            txt = _text(m.content)
            if txt:
                blocks.append({"text": txt})
            for tc in (getattr(m, "tool_calls", None) or []):
                blocks.append({"toolUse": {
                    "toolUseId": tc.get("id") or tc["name"],
                    "name": tc["name"],
                    "input": tc.get("args", {}),
                }})
            _append("assistant", blocks)
        elif cls == "ToolMessage":
            _append("user", [{"toolResult": {
                "toolUseId": getattr(m, "tool_call_id", "") or "",
                "content": [{"text": _text(m.content)}],
            }}])
    return system, out


def _from_converse(resp: dict) -> ChatResult:
    """Parse a Converse response into a ChatResult with one AIMessage."""
    content_blocks = (resp.get("output", {}).get("message", {}) or {}).get("content", []) or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append({
                "name": tu.get("name", ""),
                "args": tu.get("input", {}) or {},
                "id": tu.get("toolUseId", ""),
                "type": "tool_call",
            })
    msg = AIMessage(
        content=" ".join(text_parts),
        tool_calls=tool_calls,
        response_metadata={"stop_reason": resp.get("stopReason"), "usage": resp.get("usage", {})},
    )
    return ChatResult(generations=[ChatGeneration(message=msg)])


def _text(content: Any) -> str:
    """Flatten LangChain message content (str or list of typed parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in content]
        return "".join(parts)
    return str(content or "")
