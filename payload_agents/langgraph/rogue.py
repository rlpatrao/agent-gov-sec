"""payload_agents.langgraph.rogue — Rogue on the LangGraph framework.

The failure-path persona: valid NHI but absent from every policy set (deny-all
data, empty tool allow-list, no A2A recipients, credential_mode=deny, tiny
budget). Given a shell_exec tool on purpose — every call must be blocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import tool

from payload_agents._lib import personas
from payload_agents.langgraph._runner import LangGraphAgentBundle, build_langgraph_agent


async def build_rogue_agent(
    run_id: str,
    model: BaseChatModel,
    *,
    catalog=None,
    drift_baseline_path: Optional[Path] = None,
) -> LangGraphAgentBundle:
    catalog = catalog or personas.load_catalog()
    # Mediator is built so FGAC is active; 'Rogue' has no policy → deny-all.
    mediator = personas.make_mediator(catalog, drift_baseline_path)
    tools = [tool(personas.shell_exec)]
    return await build_langgraph_agent("rogue", run_id, model=model, tools=tools, catalog=catalog, mediator=mediator)
