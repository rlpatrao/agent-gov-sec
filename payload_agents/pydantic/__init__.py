"""payload_agents.pydantic — the demo personas on the Pydantic AI framework.

Each persona builds a governed agent via the pydantic _runner (a GovernedModel
wrapper that runs the agnostic-core GuardPipeline around each request). Exposes
the uniform framework surface (make_model + build_*). Selected by
--framework pydantic / GALAXY_FRAMEWORK. Requires the .[pydantic] extra.
"""

from __future__ import annotations

from payload_agents._lib.scripting import to_script_steps
from payload_agents._runtime.contract import ScriptStep
from payload_agents.pydantic._runner import GovernedModel, PydanticAgentBundle, build_agent
from payload_agents.pydantic.auditor import build_auditor_agent
from payload_agents.pydantic.finops import build_finops_agent
from payload_agents.pydantic.rogue import build_rogue_agent


def make_model(*messages):
    """Offline deterministic model: a Pydantic AI FunctionModel replaying the
    scripted turns (mirrors the langgraph FakeToolCallingModel / raw ScriptedChatClient)."""
    return _function_model(to_script_steps(messages))


def _function_model(steps: list[ScriptStep]):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    state = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        i = min(state["i"], len(steps) - 1) if steps else 0
        state["i"] += 1
        step = steps[i] if steps else ScriptStep()
        parts: list = [
            ToolCallPart(tool_name=tc.name, args=tc.args, tool_call_id=tc.id or f"c{j}")
            for j, tc in enumerate(step.tool_calls)
        ]
        if step.text:
            parts.append(TextPart(content=step.text))
        return ModelResponse(parts=parts)

    return FunctionModel(fn)


__all__ = [
    "make_model", "build_finops_agent", "build_auditor_agent", "build_rogue_agent",
    "GovernedModel", "PydanticAgentBundle", "build_agent",
]
