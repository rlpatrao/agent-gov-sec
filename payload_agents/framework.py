"""
payload_agents.framework — framework-axis dispatch for the demo personas.

The demo matrix is written once against a framework-neutral bundle contract
(``adapters.contract.AgentBundle`` / ``RunResult``). This module lets the SAME
matrix run under any framework on the ``--framework`` axis:

  - ``langgraph`` — the existing ``build_langgraph_agent`` path (unchanged).
  - ``raw``       — the provider-native tool loop (``adapters.raw.build_agent``).
  - ``pydantic``  — the Pydantic AI binding (``adapters.pydantic_ai.build_agent``).

The demo scripts each model turn as a LangChain ``AIMessage`` (its native, oldest
shape). ``make_model(framework, *messages)`` translates that single source of
scripted turns into whatever the chosen framework's deterministic model needs:

  - langgraph → a ``FakeToolCallingModel`` (replays the ``AIMessage`` list)
  - raw       → a ``ScriptedChatClient`` (replays neutral ``ScriptStep``)
  - pydantic  → a ``FunctionModel`` (replays ``ModelResponse`` parts)

Every framework's ``build_agent`` wires the **same shared ``GuardPipeline``** (via
``build_guard_pipeline`` / ``build_langgraph_governance``), so the governance under
test is identical — only the orchestration engine differs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage

from adapters.contract import ScriptStep, ToolCall
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_drift import DataAccessDriftDetector, JsonFileBaselineStore
from governance.extensions.data_fgac import DataAccessMediator
from payload_agents import auditor_agent, finops_agent, rogue_agent


# ── scripted-turn translation ─────────────────────────────────────────────────
def _to_script_steps(messages: tuple[AIMessage, ...]) -> list[ScriptStep]:
    """LangChain scripted ``AIMessage`` turns → framework-neutral ``ScriptStep``."""
    steps: list[ScriptStep] = []
    for m in messages:
        calls = [
            ToolCall(name=tc.get("name", ""), args=tc.get("args", {}) or {}, id=tc.get("id", "") or "")
            for tc in (getattr(m, "tool_calls", None) or [])
        ]
        steps.append(ScriptStep(text=_text(m.content), tool_calls=calls))
    return steps


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def make_model(framework: str, *messages: AIMessage):
    """Build the deterministic offline model for ``framework`` from the scripted
    ``AIMessage`` turns. (Real-model mode is handled by the demo before this is
    reached — these are the ``--fake`` / offline models.)"""
    if framework == "langgraph":
        from adapters.langgraph.runtime import scripted_model
        return scripted_model(*messages)
    if framework == "raw":
        from adapters.raw import ScriptedChatClient
        return ScriptedChatClient(_to_script_steps(messages))
    if framework == "pydantic":
        return _pydantic_function_model(_to_script_steps(messages))
    raise ValueError(f"unknown framework: {framework}")


def _pydantic_function_model(steps: list[ScriptStep]):
    """A Pydantic AI ``FunctionModel`` that replays ``steps`` (clamping at the
    last), mirroring the FakeToolCallingModel / ScriptedChatClient."""
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


# ── per-persona, per-framework builders ───────────────────────────────────────
def _drift_mediator(catalog: DataClassificationCatalog, drift_baseline_path: Optional[Path]) -> DataAccessMediator:
    drift = DataAccessDriftDetector(store=JsonFileBaselineStore(drift_baseline_path))
    return DataAccessMediator(catalog=catalog, drift_detector=drift)


async def build_finops_agent(framework: str, run_id: str, model: Any, *, drift_baseline_path: Optional[Path] = None):
    if framework == "langgraph":
        return await finops_agent.build_finops_agent(run_id, model, drift_baseline_path=drift_baseline_path)
    catalog = finops_agent.load_catalog()
    mediator = _drift_mediator(catalog, drift_baseline_path)
    nhi_id = "local-finops-nhi"
    specs = finops_agent.make_tool_specs(mediator=mediator, nhi_id=nhi_id)
    build = _adapter_build(framework)
    return await build("finops", "FinOps", run_id, model=model, tool_specs=specs, mediator=mediator, catalog=catalog)


async def build_auditor_agent(framework: str, run_id: str, model: Any, *, drift_baseline_path: Optional[Path] = None):
    if framework == "langgraph":
        return await auditor_agent.build_auditor_agent(run_id, model, drift_baseline_path=drift_baseline_path)
    catalog = auditor_agent.load_catalog()
    mediator = _drift_mediator(catalog, drift_baseline_path)
    nhi_id = "local-auditor-nhi"
    specs = auditor_agent.make_tool_specs(mediator=mediator, nhi_id=nhi_id)
    build = _adapter_build(framework)
    return await build("auditor", "Auditor", run_id, model=model, tool_specs=specs, mediator=mediator, catalog=catalog)


async def build_rogue_agent(framework: str, run_id: str, model: Any, *, drift_baseline_path: Optional[Path] = None):
    if framework == "langgraph":
        return await rogue_agent.build_rogue_agent(run_id, model, drift_baseline_path=drift_baseline_path)
    catalog = rogue_agent.load_catalog()
    mediator = _drift_mediator(catalog, drift_baseline_path)
    specs = rogue_agent.make_tool_specs()
    build = _adapter_build(framework)
    return await build("rogue", "Rogue", run_id, model=model, tool_specs=specs, mediator=mediator, catalog=catalog)


def _adapter_build(framework: str):
    if framework == "raw":
        from adapters.raw import build_agent
        return build_agent
    if framework == "pydantic":
        from adapters.pydantic_ai import build_agent
        return build_agent
    raise ValueError(f"unknown framework: {framework}")
