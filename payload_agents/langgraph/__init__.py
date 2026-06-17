"""payload_agents.langgraph — the demo personas on the LangGraph framework.

Each persona builds a governed agent via the langgraph _runner (LangChain
create_agent + the GalaxyGuardMiddleware shim over the agnostic-core GuardPipeline).
Exposes the uniform framework surface the demo dispatches on: make_model + the
three build_* coroutines. Selected by --framework langgraph / GALAXY_FRAMEWORK.
"""

from __future__ import annotations

from payload_agents._runtime.models import scripted_model
from payload_agents.langgraph.auditor import build_auditor_agent
from payload_agents.langgraph.finops import build_finops_agent
from payload_agents.langgraph.rogue import build_rogue_agent


def make_model(*messages):
    """Offline deterministic model for this framework (replays scripted AIMessages)."""
    return scripted_model(*messages)


__all__ = ["make_model", "build_finops_agent", "build_auditor_agent", "build_rogue_agent"]
