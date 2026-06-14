"""
adapters.raw — the provider-native (no-framework) binding.

The "control" arm of the framework axis: a hand-rolled tool loop that calls a
``ChatModelClient`` directly and runs the **same shared `GuardPipeline`** around
each step — no LangChain, no Pydantic AI, no agent framework at all. It proves
the governance is genuinely framework-neutral: the identical before_model /
before_tool / after_model sequence governs a bare loop just as it governs
LangGraph middleware or a Pydantic AI model wrapper.

  RawAgentBundle.invoke(prompt):
      loop (capped):
        pipeline.before_model(text)          (B4/B5/B6 — raises to block)
        res = client.generate(messages, tool_specs)
        pipeline.after_model(res.text)       (CoT/CoVe capture, G20)
        for each tool call:
          pipeline.before_tool(name, args)   (B7/G19/B8 — raises to block)
          run the tool fn, feed the result back into messages

``build_agent`` mirrors ``adapters.pydantic_ai.build_agent``: it resolves the
NHI, consults the egress chokepoint, and builds the guard pipeline from the
per-agent YAML — only the execution engine differs. ``model`` here is a
``ChatModelClient`` (a ``ScriptedChatClient`` for ``--fake``; a best-effort live
client otherwise).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from adapters.contract import (
    ChatModelClient,
    ModelResult,
    RunResult,
    ScriptStep,
    ToolCall,
    ToolSpec,
    Turn,
)
from core.nhi_registry import NHIRegistry
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_fgac import DataAccessMediator
from governance.pipeline import GuardPipeline, build_guard_pipeline
from payload_agents.config import load_agent_config_cached

logger = logging.getLogger(__name__)

_MAX_ITERS = 6


class ScriptedChatClient:
    """A deterministic ``ChatModelClient`` for ``--fake`` runs: replays a list of
    ``ScriptStep`` in order, clamping at the last step (mirrors the LangGraph
    ``FakeToolCallingModel`` and the Pydantic AI ``FunctionModel``)."""

    def __init__(self, steps: list[ScriptStep]) -> None:
        self._steps = list(steps)
        self._cursor = 0

    def generate(self, messages: list[dict], tool_specs: list[ToolSpec]) -> ModelResult:
        if not self._steps:
            return ModelResult(text="", tool_calls=[])
        i = min(self._cursor, len(self._steps) - 1)
        self._cursor += 1
        step = self._steps[i]
        return ModelResult(text=step.text or "", tool_calls=list(step.tool_calls))


@dataclass(frozen=True)
class RawAgentBundle:
    """Framework-neutral bundle (mirrors LangGraphAgentBundle / PydanticAgentBundle),
    but holds no framework agent object — just the client + tool specs + pipeline."""
    client: ChatModelClient
    tool_specs: list[ToolSpec]
    pipeline: GuardPipeline
    mediator: Optional[DataAccessMediator]
    pg_backend: Any
    audit_logger: Any
    config: Any
    agent_id: str
    nhi_id: str
    egress: str

    def invoke(self, prompt: str) -> RunResult:
        """Run the provider-native tool loop on ``prompt``, returning a
        framework-neutral ``RunResult``. A blocking guard raises
        ``GovernanceViolation`` (propagated) exactly as on the other axes."""
        fns = {s.name: s.fn for s in self.tool_specs}
        msgs: list[dict] = [{"role": "user", "content": prompt}]
        turns: list[Turn] = []

        for _ in range(_MAX_ITERS):
            # The text the model is about to act on: every user + tool message so far.
            text = " ".join(
                str(m.get("content", "")) for m in msgs
                if m.get("role") in ("user", "tool") and m.get("content")
            ).strip()
            self.pipeline.before_model(text)          # B4/B5/B6 — raises to block

            res = self.client.generate(msgs, self.tool_specs)
            # after_model returns the (possibly output-redacted) text — G20 trace
            # plus output guards (content quality, output PII).
            res_text = self.pipeline.after_model(res.text or "")

            if res_text:
                turns.append(Turn(role="ai", text=res_text))

            if res.tool_calls:
                turns.append(Turn(role="ai", tool_calls=list(res.tool_calls)))
                msgs.append({
                    "role": "assistant",
                    "content": res.text or "",
                    "tool_calls": [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in res.tool_calls],
                })
                for tc in res.tool_calls:
                    self.pipeline.before_tool(tc.name, tc.args)   # B7/G19/B8 + sweep before_tool guards
                    fn = fns.get(tc.name)
                    if fn is None:
                        out = f"(no such tool: {tc.name})"
                    else:
                        try:
                            out = fn(**(tc.args or {}))
                        except Exception:
                            self.pipeline.on_tool_error(tc.name)   # circuit-breaker failure record
                            raise
                    # after_tool: inbound tool-output governance (e.g. MCP response
                    # scan) + circuit-breaker success record; may sanitize the result.
                    out = self.pipeline.after_tool(tc.name, str(out))
                    turns.append(Turn(role="tool", text=str(out), tool_name=tc.name))
                    msgs.append({"role": "tool", "content": str(out), "tool_call_id": tc.id})
                continue
            break

        return RunResult(turns=turns)


def _resolve_egress(agent_type: str, client_id: str) -> str:
    try:
        from core.provider_factory import get_provider
        return get_provider().llm_gateway().resolve(agent_type=agent_type, client_id=client_id).mode
    except Exception as e:
        logger.info("raw.egress.offline", extra={"agent_type": agent_type, "reason": str(e)[:80]})
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
) -> RawAgentBundle:
    """Build a governed provider-native agent. ``model`` is a ``ChatModelClient``
    (a ``ScriptedChatClient`` for ``--fake``)."""
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

    logger.info("raw.agent.built", extra={"run_id": run_id, "agent_id": agent_id, "egress": egress})
    return RawAgentBundle(
        client=model, tool_specs=list(tool_specs), pipeline=pipeline,
        mediator=mediator, pg_backend=pg_backend, audit_logger=audit,
        config=cfg, agent_id=agent_id, nhi_id=identity.client_id, egress=egress,
    )


__all__ = ["ScriptedChatClient", "RawAgentBundle", "build_agent"]
