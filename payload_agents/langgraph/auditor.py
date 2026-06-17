"""payload_agents.langgraph.auditor — Auditor on the LangGraph framework.

Privileged cross-dataset reader and A2A callee. Its query_dataset tool reads
finops + hr through the FGAC mediator up to CONFIDENTIAL clearance; RESTRICTED
columns (ssn, tax_id) stay masked. Governed by the shared GuardPipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import tool

from payload_agents._lib import personas
from payload_agents.langgraph._runner import LangGraphAgentBundle, build_langgraph_agent


async def build_auditor_agent(
    run_id: str,
    model: BaseChatModel,
    *,
    catalog=None,
    drift_baseline_path: Optional[Path] = None,
) -> LangGraphAgentBundle:
    catalog = catalog or personas.load_catalog()
    mediator = personas.make_mediator(catalog, drift_baseline_path)
    query_dataset, summarize_costs = personas.auditor_callables(mediator=mediator, nhi_id="local-auditor-nhi")
    tools = [tool(query_dataset), tool(summarize_costs)]
    return await build_langgraph_agent("auditor", run_id, model=model, tools=tools, catalog=catalog, mediator=mediator)
