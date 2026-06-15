"""
payload_agents.auditor_agent — Auditor (LangGraph demo persona).

The privileged cross-dataset reader and A2A callee. Its ``query_dataset`` tool
reads finops + hr through the FGAC mediator up to CONFIDENTIAL clearance;
RESTRICTED columns (``ssn``, ``tax_id``) stay masked even for the Auditor. Used
as the recipient of FinOpsAnalyst's A2A dispatch so the full governed hop is
demonstrated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import tool

from agent_framework_adapters.langgraph._base import LangGraphAgentBundle, build_langgraph_agent
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_drift import DataAccessDriftDetector, JsonFileBaselineStore
from governance.extensions.data_fgac import DataAccessMediator
from payload_agents._lib import demo_data
from payload_agents.finops_agent import load_catalog

AGENT_NAME = "auditor"
AGENT_TYPE = "Auditor"


def _dataset_fns(*, mediator: DataAccessMediator, nhi_id: str):
    """The persona's tool *logic* (framework-neutral plain functions)."""

    def query_dataset(dataset: str, table: str, columns: list[str]) -> str:
        """Read rows from a governed dataset.table. Classification/category
        masking is enforced; RESTRICTED columns remain masked."""
        decision, rows = mediator.read(
            agent_type=AGENT_TYPE, dataset=dataset, table=table,
            columns=list(columns), rows=demo_data.rows_for(dataset, table), nhi_id=nhi_id,
        )
        return json.dumps({
            "denied": decision.denied,
            "masked_columns": list(decision.masked_columns),
            "allowed_columns": list(decision.allowed_columns),
            "rows": rows,
        })

    def summarize_costs(text: str) -> str:
        """Summarize fetched data into a one-line audit note."""
        return f"Audit note: {text[:160]}"

    return query_dataset, summarize_costs


def make_tool_specs(*, mediator: DataAccessMediator, nhi_id: str):
    """Framework-neutral ToolSpecs (used by the raw / pydantic adapters)."""
    from agent_framework_adapters.contract import ToolSpec
    query_dataset, summarize_costs = _dataset_fns(mediator=mediator, nhi_id=nhi_id)
    return [
        ToolSpec(name="query_dataset", description=query_dataset.__doc__ or "Read a governed dataset.table.",
                 parameters={"type": "object",
                             "properties": {"dataset": {"type": "string"}, "table": {"type": "string"},
                                            "columns": {"type": "array", "items": {"type": "string"}}},
                             "required": ["dataset", "table", "columns"]}, fn=query_dataset),
        ToolSpec(name="summarize_costs", description="Summarize fetched data into a one-line audit note.",
                 parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                 fn=summarize_costs),
    ]


def make_tools(*, mediator: DataAccessMediator, nhi_id: str):
    @tool
    def query_dataset(dataset: str, table: str, columns: list[str]) -> str:
        """Read rows from a governed dataset.table. Classification/category
        masking is enforced; RESTRICTED columns remain masked."""
        decision, rows = mediator.read(
            agent_type=AGENT_TYPE, dataset=dataset, table=table,
            columns=columns, rows=demo_data.rows_for(dataset, table), nhi_id=nhi_id,
        )
        return json.dumps({
            "denied": decision.denied,
            "masked_columns": list(decision.masked_columns),
            "allowed_columns": list(decision.allowed_columns),
            "rows": rows,
        })

    @tool
    def summarize_costs(text: str) -> str:
        """Summarize fetched data into a one-line audit note."""
        return f"Audit note: {text[:160]}"

    return [query_dataset, summarize_costs]


async def build_auditor_agent(
    run_id: str,
    model: BaseChatModel,
    *,
    catalog: Optional[DataClassificationCatalog] = None,
    drift_baseline_path: Optional[Path] = None,
) -> LangGraphAgentBundle:
    catalog = catalog or load_catalog()
    drift = DataAccessDriftDetector(store=JsonFileBaselineStore(drift_baseline_path))
    mediator = DataAccessMediator(catalog=catalog, drift_detector=drift)
    nhi_id = "local-auditor-nhi"
    tools = make_tools(mediator=mediator, nhi_id=nhi_id)
    return await build_langgraph_agent(
        AGENT_NAME, run_id, model=model, tools=tools, catalog=catalog, mediator=mediator,
    )
