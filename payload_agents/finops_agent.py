"""
payload_agents.finops_agent — FinOpsAnalyst (LangGraph demo persona).

The happy-path / partial-restriction star. A scoped data-reading agent whose
``query_billing`` tool reads ``finops.billing`` **through the FGAC mediator**, so
the demo shows real column-masking (``customer_email``, above-clearance
``tax_id``) and row-filtering (US regions only) on the SUCCESS path. Governed by
the same ``GalaxyGuardMiddleware`` stack as every other agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

# The billing table's real columns — constrains the tool's `columns` argument to a
# JSON-schema enum so a real LLM requests catalog columns (and the FGAC masking of
# customer_email/tax_id is exercised) instead of inventing names like "total_cost".
BillingColumn = Literal["account_id", "cost_usd", "region", "customer_email", "tax_id"]

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import tool

from adapters.langgraph._base import LangGraphAgentBundle, build_langgraph_agent
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_drift import DataAccessDriftDetector, JsonFileBaselineStore
from governance.extensions.data_fgac import DataAccessMediator
from payload_agents._lib import demo_data

AGENT_NAME = "finops"
AGENT_TYPE = "FinOps"

_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "governance" / "extensions" / "configs" / "data-classification.example.yaml"
)


def load_catalog() -> DataClassificationCatalog:
    return DataClassificationCatalog.load(_CATALOG_PATH)


def make_tools(*, mediator: DataAccessMediator, nhi_id: str):
    """Build FinOps tools as closures over the shared FGAC mediator."""

    @tool
    def query_billing(columns: list[BillingColumn]) -> str:
        """Read finops.billing rows for the requested columns. The billing table has
        exactly these columns: account_id, cost_usd, region, customer_email, tax_id —
        request from these (e.g. account_id, cost_usd, region). The data layer enforces
        column masking (customer_email, tax_id) and US-region row-filtering."""
        decision, rows = mediator.read(
            agent_type=AGENT_TYPE, dataset="finops", table="billing",
            columns=columns, rows=demo_data.BILLING, nhi_id=nhi_id,
        )
        return json.dumps({
            "denied": decision.denied,
            "masked_columns": list(decision.masked_columns),
            "allowed_columns": list(decision.allowed_columns),
            "rows": rows,
        })

    @tool
    def summarize_costs(text: str) -> str:
        """Summarize fetched cost data into a one-line report."""
        return f"FinOps summary: {text[:160]}"

    return [query_billing, summarize_costs]


async def build_finops_agent(
    run_id: str,
    model: BaseChatModel,
    *,
    catalog: Optional[DataClassificationCatalog] = None,
    drift_baseline_path: Optional[Path] = None,
) -> LangGraphAgentBundle:
    """Build the governed FinOpsAnalyst. One mediator (with drift) is shared
    between the tools and the guard stack."""
    catalog = catalog or load_catalog()
    drift = DataAccessDriftDetector(store=JsonFileBaselineStore(drift_baseline_path))
    mediator = DataAccessMediator(catalog=catalog, drift_detector=drift)
    nhi_id = "local-finops-nhi"
    tools = make_tools(mediator=mediator, nhi_id=nhi_id)
    return await build_langgraph_agent(
        AGENT_NAME, run_id, model=model, tools=tools, catalog=catalog, mediator=mediator,
    )
