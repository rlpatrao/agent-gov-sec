"""
payload_agents.langgraph._guard — the LangGraph framework binding for governance.

``GalaxyGuardMiddleware`` is a **thin** LangChain ``AgentMiddleware`` shim: it maps
LangChain's ``wrap_model_call`` / ``wrap_tool_call`` hooks onto the
framework-neutral ``governance.pipeline.GuardPipeline``. No governance *logic*
lives here — the orchestration (prompt-injection, credential, budget, reasoning
trace, capability, blocked-pattern) and the audit logging are all in the
pipeline, shared verbatim with the raw and Pydantic AI framework adapters. This
file only translates LangChain's request/response objects to/from plain text and
applies in-place credential redaction on LangChain messages.

``GovernanceViolation`` is defined in ``governance.pipeline`` and re-exported here
for backward-compatible imports.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from langchain.agents.middleware import AgentMiddleware

from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_fgac import DataAccessMediator
from governance.pipeline import GovernanceViolation, GuardPipeline, build_guard_pipeline

__all__ = ["GalaxyGuardMiddleware", "GovernanceViolation", "build_langgraph_governance"]

logger = logging.getLogger(__name__)


def _model_input_text(request: Any) -> str:
    """Extract the concatenated user-message text from a LangChain ModelRequest,
    resilient to content being a string or a list of content blocks."""
    messages = getattr(request, "messages", None) or []
    parts: list[str] = []
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    parts.append(block["text"])
    return " ".join(p for p in parts if p).strip()


def _response_text(response: Any) -> str:
    msg = getattr(response, "message", None) or getattr(response, "result", None) or response
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


class GalaxyGuardMiddleware(AgentMiddleware):
    """LangChain ``AgentMiddleware`` that runs the shared ``GuardPipeline`` at the
    model-call and tool-call boundaries."""

    def __init__(self, pipeline: GuardPipeline) -> None:
        self._pipeline = pipeline

    # ── per-model-call governance (B4/B5/B6 + G20) ──────────────────────────
    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        text = _model_input_text(request)
        if self._pipeline.before_model(text):   # True → redact credentials in place
            self._redact_in_place(request)
        response = handler(request)
        self._pipeline.after_model(_response_text(response))
        return response

    # ── per-tool-call governance (B7/G19 + B8 + sweep before_tool/after_tool) ─
    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name", "<unknown>") if isinstance(tool_call, dict) else str(tool_call)
        args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}
        self._pipeline.before_tool(name, args)
        try:
            result = handler(request)
        except Exception:
            self._pipeline.on_tool_error(name)   # circuit-breaker failure record
            raise
        # after_tool: inbound tool-output governance (MCP response scan) +
        # circuit-breaker success record. A blocking output guard raises; the
        # sanitized text is applied on the text-carrying axes (raw / pydantic).
        self._pipeline.after_tool(name, _response_text(result))
        return result

    def _redact_in_place(self, request: Any) -> None:
        redactor = self._pipeline.redactor
        if redactor is None:
            return
        messages = getattr(request, "messages", None) or []
        for msg in messages:
            content = getattr(msg, "content", None)
            if isinstance(content, str) and redactor.contains_credentials(content):
                try:
                    msg.content = redactor.redact(content)
                except Exception:  # immutable message — audit already records the redaction intent
                    pass


async def build_langgraph_governance(
    *,
    agent_id: str,
    agent_type: str,
    nhi_id: str,
    run_id: str,
    allowed_tools: Optional[list[str]] = None,
    blocked_patterns: Optional[list[str]] = None,
    prompt_injection_block_threshold: str = "medium",
    enable_prompt_injection_guard: bool = True,
    enable_credential_redactor: bool = True,
    credential_mode: str = "redact",
    enable_context_budget: bool = True,
    context_budget_tokens: int = 8000,
    enable_data_fgac: bool = False,
    enable_data_drift: bool = False,
    enable_reasoning_guard: bool = False,
    enable_reasoning_trace: bool = False,
    catalog: Optional[DataClassificationCatalog] = None,
    mediator: Optional[DataAccessMediator] = None,
) -> tuple[list, Any, Any, DataAccessMediator | None]:
    """Assemble the governance middleware for a LangGraph agent.

    Builds the shared ``GuardPipeline`` (via ``governance.pipeline.build_guard_pipeline``)
    and wraps it in a single ``GalaxyGuardMiddleware``. Returns
    ``(middleware_list, pg_backend, audit_logger, mediator)`` — unchanged surface."""
    pipeline, ledger, audit, mediator = await build_guard_pipeline(
        agent_id=agent_id, agent_type=agent_type, nhi_id=nhi_id, run_id=run_id,
        allowed_tools=allowed_tools, blocked_patterns=blocked_patterns,
        prompt_injection_block_threshold=prompt_injection_block_threshold,
        enable_prompt_injection_guard=enable_prompt_injection_guard,
        enable_credential_redactor=enable_credential_redactor, credential_mode=credential_mode,
        enable_context_budget=enable_context_budget, context_budget_tokens=context_budget_tokens,
        enable_data_fgac=enable_data_fgac, enable_data_drift=enable_data_drift,
        enable_reasoning_guard=enable_reasoning_guard, enable_reasoning_trace=enable_reasoning_trace,
        catalog=catalog, mediator=mediator,
    )
    return [GalaxyGuardMiddleware(pipeline)], ledger, audit, mediator
