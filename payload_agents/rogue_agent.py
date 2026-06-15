"""
payload_agents.rogue_agent — Rogue / UntrustedExtractor (LangGraph demo persona).

The failure-path star. It has a valid NHI (so it builds) but is absent from every
policy set: no ABAC data policy (the mediator denies-all for ``Rogue``), an empty
tool allow-list (any tool call is denied by the reasoning-step guard), and no A2A
recipients. ``credential_mode=deny`` and a tiny context budget make its
secret-bearing / oversized prompts trip those guards too.

It is given a ``shell_exec`` tool *on purpose* — every attempt to call it must be
blocked. Governance does not rely on the agent cooperating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import tool

from agent_framework_adapters.langgraph._base import LangGraphAgentBundle, build_langgraph_agent
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_drift import DataAccessDriftDetector, JsonFileBaselineStore
from governance.extensions.data_fgac import DataAccessMediator
from payload_agents.finops_agent import load_catalog

AGENT_NAME = "rogue"
AGENT_TYPE = "Rogue"


def _shell_exec(cmd: str) -> str:
    """Run a shell command. (Never permitted — present only to be denied.)"""
    return f"(should never run) {cmd}"


def make_tools():
    return [tool(_shell_exec)]


def make_tool_specs():
    """Framework-neutral ToolSpecs (used by the raw / pydantic adapters)."""
    from agent_framework_adapters.contract import ToolSpec
    return [ToolSpec(name="shell_exec", description=_shell_exec.__doc__ or "Run a shell command.",
                     parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
                     fn=_shell_exec)]


async def build_rogue_agent(
    run_id: str,
    model: BaseChatModel,
    *,
    catalog: Optional[DataClassificationCatalog] = None,
    drift_baseline_path: Optional[Path] = None,
) -> LangGraphAgentBundle:
    catalog = catalog or load_catalog()
    drift = DataAccessDriftDetector(store=JsonFileBaselineStore(drift_baseline_path))
    # Mediator is built so FGAC is active; 'Rogue' has no policy → deny-all.
    mediator = DataAccessMediator(catalog=catalog, drift_detector=drift)
    return await build_langgraph_agent(
        AGENT_NAME, run_id, model=model, tools=make_tools(), catalog=catalog, mediator=mediator,
    )
