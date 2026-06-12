"""
adapters.pydantic_ai — the Pydantic AI framework binding.

Third member of the framework axis (alongside ``langgraph`` and ``raw``). Pydantic
AI is a minimal, typed, model-agnostic agent framework, so it reuses the
cloud-resolved credentials via its own native model classes — no hand-rolled
client. Governance is the **same shared `GuardPipeline`**, wired through a thin
model wrapper (no LangChain, no second tracer):

  GovernedModel.request()  → pipeline.before_model(text)        (B4/B5/B6)
                           → inner model request
                           → pipeline.before_tool(name, args)   (B7/G19/B8) per tool call
                           → pipeline.after_model(response text) (CoT/CoVe, G20)

Running governance in the wrapper (rather than wrapping each tool fn) keeps the
tools as raw typed functions, so their JSON-schema (incl. the FinOps column enum)
survives. A blocking guard raises ``GovernanceViolation``, which propagates out of
``Agent.run_sync`` — caught uniformly by the demo, exactly as on the other axes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic_ai import Agent
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.tools import Tool

from adapters.contract import RunResult, ToolCall, ToolSpec, Turn
from core.nhi_registry import NHIRegistry
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_fgac import DataAccessMediator
from governance.pipeline import GuardPipeline, build_guard_pipeline
from payload_agents.config import load_agent_config_cached

logger = logging.getLogger(__name__)


def _messages_text(messages: list) -> str:
    """Concatenate the user-prompt + tool-return text from a pydantic-ai message
    history — the text the model is about to act on (what before_model inspects)."""
    parts: list[str] = []
    for m in messages:
        for p in getattr(m, "parts", []):
            cls = type(p).__name__
            if cls in ("UserPromptPart", "ToolReturnPart", "RetryPromptPart"):
                c = getattr(p, "content", "")
                parts.append(c if isinstance(c, str) else str(c))
    return " ".join(s for s in parts if s).strip()


def _response_text(response: Any) -> str:
    return " ".join(
        p.content for p in getattr(response, "parts", [])
        if type(p).__name__ == "TextPart" and getattr(p, "content", "")
    )


class GovernedModel(WrapperModel):
    """Wraps any pydantic-ai model and runs the shared GuardPipeline around each
    request — before_model on the input, before_tool on each emitted tool call,
    after_model on the response."""

    def __init__(self, wrapped: Any, pipeline: GuardPipeline) -> None:
        super().__init__(wrapped)
        self._pipeline = pipeline

    async def request(self, messages, model_settings, model_request_parameters):
        self._pipeline.before_model(_messages_text(messages))   # B4/B5/B6 — raises to block
        response = await super().request(messages, model_settings, model_request_parameters)
        for part in getattr(response, "parts", []):
            if type(part).__name__ == "ToolCallPart":
                args = part.args
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except ValueError:
                        args = {"_raw": args}
                self._pipeline.before_tool(part.tool_name, args or {})   # B7/G19/B8 — raises to block
        self._pipeline.after_model(_response_text(response))    # G20 CoT/CoVe trace
        return response


@dataclass(frozen=True)
class PydanticAgentBundle:
    """Framework-neutral bundle (mirrors LangGraphAgentBundle)."""
    agent: Any
    pg_backend: Any
    audit_logger: Any
    mediator: Optional[DataAccessMediator]
    config: Any
    agent_id: str
    nhi_id: str
    egress: str

    def invoke(self, prompt: str) -> RunResult:
        """Run the agent synchronously and return a framework-neutral RunResult.

        Pydantic AI's ``run_sync`` drives its own event loop, which raises if one is
        already running (the demo invokes bundles synchronously from inside an async
        ``main``). When a loop is already running we run the agent's coroutine on a
        dedicated worker thread so the call stays synchronous regardless of caller
        context — and a blocking ``GovernanceViolation`` still propagates out."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _normalize(self.agent.run_sync(prompt))

        out: dict[str, Any] = {}

        def _worker() -> None:
            try:
                out["result"] = asyncio.run(self.agent.run(prompt))
            except BaseException as e:  # propagate GovernanceViolation et al.
                out["error"] = e

        import threading
        t = threading.Thread(target=_worker)
        t.start()
        t.join()
        if "error" in out:
            raise out["error"]
        return _normalize(out["result"])


def _normalize(result: Any) -> RunResult:
    """pydantic-ai run messages → framework-neutral RunResult."""
    turns: list[Turn] = []
    for m in result.all_messages():
        for p in getattr(m, "parts", []):
            cls = type(p).__name__
            if cls == "TextPart" and getattr(p, "content", ""):
                turns.append(Turn(role="ai", text=p.content))
            elif cls == "ToolCallPart":
                args = p.args
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except ValueError:
                        args = {}
                turns.append(Turn(role="ai", tool_calls=[ToolCall(name=p.tool_name, args=args or {}, id=p.tool_call_id or "")]))
            elif cls == "ToolReturnPart":
                turns.append(Turn(role="tool", text=str(getattr(p, "content", "")), tool_name=getattr(p, "tool_name", "") or ""))
    return RunResult(turns=turns)


def _resolve_egress(agent_type: str, client_id: str) -> str:
    try:
        from core.provider_factory import get_provider
        return get_provider().llm_gateway().resolve(agent_type=agent_type, client_id=client_id).mode
    except Exception as e:
        logger.info("pydantic_ai.egress.offline", extra={"agent_type": agent_type, "reason": str(e)[:80]})
        return "offline-no-egress"


async def build_agent(
    agent_name: str,
    agent_type: str,
    run_id: str,
    *,
    model: Any,
    tool_specs: list[ToolSpec],
    system_prompt: str = "",
    mediator: Optional[DataAccessMediator] = None,
    catalog: Optional[DataClassificationCatalog] = None,
) -> PydanticAgentBundle:
    """Build a governed Pydantic AI agent. ``model`` is a pydantic-ai model (a
    native cloud model for live runs, or a FunctionModel/TestModel for --fake)."""
    cfg = load_agent_config_cached(agent_name)
    identity = NHIRegistry.get(agent_type)
    agent_id = f"{agent_type}-{identity.client_id}"
    egress = _resolve_egress(agent_type, identity.client_id)

    g = cfg.governance
    pipeline, pg_backend, audit, mediator = await build_guard_pipeline(
        agent_id=agent_id, agent_type=agent_type, nhi_id=identity.client_id, run_id=run_id,
        allowed_tools=g.allowed_tools or None,
        blocked_patterns=getattr(g, "blocked_patterns", None) or ["DROP TABLE", "rm -rf"],
        prompt_injection_block_threshold=g.prompt_injection_block_threshold,
        enable_prompt_injection_guard=g.enable_prompt_injection_guard,
        enable_credential_redactor=g.enable_credential_redactor, credential_mode=g.credential_mode,
        enable_context_budget=g.enable_context_budget, context_budget_tokens=g.context_budget_tokens,
        enable_data_fgac=getattr(g, "enable_data_fgac", False),
        enable_data_drift=getattr(g, "enable_data_drift", False),
        enable_reasoning_guard=getattr(g, "enable_reasoning_guard", False),
        enable_reasoning_trace=getattr(g, "enable_reasoning_trace", False),
        catalog=catalog, mediator=mediator,
    )

    tools = [Tool(s.fn, name=s.name, description=s.description, takes_ctx=False) for s in tool_specs]
    agent = Agent(GovernedModel(model, pipeline), tools=tools,
                  system_prompt=system_prompt or "", retries=0)

    logger.info("pydantic_ai.agent.built", extra={"run_id": run_id, "agent_id": agent_id, "egress": egress})
    return PydanticAgentBundle(
        agent=agent, pg_backend=pg_backend, audit_logger=audit, mediator=mediator,
        config=cfg, agent_id=agent_id, nhi_id=identity.client_id, egress=egress,
    )
