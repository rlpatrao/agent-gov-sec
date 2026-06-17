"""payload_agents.langgraph.finops — FinOpsAnalyst on the LangGraph framework.

Scoped data reader. Its query_billing tool reads finops.billing through the FGAC
mediator, so column masking (customer_email, tax_id) and US-region row-filtering
are exercised. Governed by the shared GuardPipeline via the langgraph _runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import tool

from payload_agents._lib import personas
from payload_agents.langgraph._runner import LangGraphAgentBundle, build_langgraph_agent


async def build_finops_agent(
    run_id: str,
    model: BaseChatModel,
    *,
    catalog=None,
    drift_baseline_path: Optional[Path] = None,
) -> LangGraphAgentBundle:
    catalog = catalog or personas.load_catalog()
    mediator = personas.make_mediator(catalog, drift_baseline_path)
    query_billing, summarize_costs = personas.finops_callables(mediator=mediator, nhi_id="local-finops-nhi")
    tools = [tool(query_billing), tool(summarize_costs)]
    return await build_langgraph_agent("finops", run_id, model=model, tools=tools, catalog=catalog, mediator=mediator)
