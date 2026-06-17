"""
payload_agents._lib.personas — shared, framework-neutral persona logic.

The three demo personas (FinOps, Auditor, Rogue) have the same *domain* logic on
every framework: their tools read through the FGAC ``DataAccessMediator``, and
they share one catalog + drift store. That logic lives here, once, with no
framework import — each framework folder (langgraph/pydantic/raw) wraps it in its
own idiom (LangChain ``@tool``, a Pydantic AI ``Tool``, or a raw ``ToolSpec``).

Provided per persona:
  - ``*_callables(mediator, nhi_id)`` — the plain tool functions (carry the typed
    signatures + docstrings the frameworks introspect).
  - ``*_specs(mediator, nhi_id)`` — the neutral ``ToolSpec`` list (raw + pydantic).
Plus ``load_catalog`` / ``make_mediator`` shared by every build.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_drift import DataAccessDriftDetector, JsonFileBaselineStore
from governance.extensions.data_fgac import DataAccessMediator
from payload_agents._lib import demo_data
from payload_agents._runtime.contract import ToolSpec

# ── catalog + mediator (shared by every persona/framework) ─────────────────────
CATALOG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "governance" / "extensions" / "configs" / "data-classification.example.yaml"
)


def load_catalog() -> DataClassificationCatalog:
    return DataClassificationCatalog.load(CATALOG_PATH)


def make_mediator(catalog: DataClassificationCatalog, drift_baseline_path: Optional[Path] = None) -> DataAccessMediator:
    """One mediator (with drift) shared between an agent's tools and its guard stack."""
    drift = DataAccessDriftDetector(store=JsonFileBaselineStore(drift_baseline_path))
    return DataAccessMediator(catalog=catalog, drift_detector=drift)


# ── FinOps ──────────────────────────────────────────────────────────────────────
# Constrains the tool's `columns` argument to a JSON-schema enum so a real LLM
# requests catalog columns (and customer_email/tax_id masking is exercised).
BillingColumn = Literal["account_id", "cost_usd", "region", "customer_email", "tax_id"]
_BILLING_COLS = ["account_id", "cost_usd", "region", "customer_email", "tax_id"]


def finops_callables(*, mediator: DataAccessMediator, nhi_id: str):
    def query_billing(columns: list[BillingColumn]) -> str:
        """Read finops.billing rows for the requested columns. The billing table has
        exactly these columns: account_id, cost_usd, region, customer_email, tax_id —
        request from these (e.g. account_id, cost_usd, region). The data layer enforces
        column masking (customer_email, tax_id) and US-region row-filtering."""
        decision, rows = mediator.read(
            agent_type="FinOps", dataset="finops", table="billing",
            columns=list(columns), rows=demo_data.BILLING, nhi_id=nhi_id,
        )
        return json.dumps({
            "denied": decision.denied,
            "masked_columns": list(decision.masked_columns),
            "allowed_columns": list(decision.allowed_columns),
            "rows": rows,
        })

    def summarize_costs(text: str) -> str:
        """Summarize fetched cost data into a one-line report."""
        return f"FinOps summary: {text[:160]}"

    return query_billing, summarize_costs


def finops_specs(*, mediator: DataAccessMediator, nhi_id: str) -> list[ToolSpec]:
    query_billing, summarize_costs = finops_callables(mediator=mediator, nhi_id=nhi_id)
    return [
        ToolSpec(name="query_billing", description=query_billing.__doc__ or "Read finops.billing.",
                 parameters={"type": "object",
                             "properties": {"columns": {"type": "array", "items": {"type": "string", "enum": _BILLING_COLS}}},
                             "required": ["columns"]}, fn=query_billing),
        ToolSpec(name="summarize_costs", description="Summarize fetched cost data into a one-line report.",
                 parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                 fn=summarize_costs),
    ]


# ── Auditor ─────────────────────────────────────────────────────────────────────
def auditor_callables(*, mediator: DataAccessMediator, nhi_id: str):
    def query_dataset(dataset: str, table: str, columns: list[str]) -> str:
        """Read rows from a governed dataset.table. Classification/category
        masking is enforced; RESTRICTED columns remain masked."""
        decision, rows = mediator.read(
            agent_type="Auditor", dataset=dataset, table=table,
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


def auditor_specs(*, mediator: DataAccessMediator, nhi_id: str) -> list[ToolSpec]:
    query_dataset, summarize_costs = auditor_callables(mediator=mediator, nhi_id=nhi_id)
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


# ── Rogue ───────────────────────────────────────────────────────────────────────
def shell_exec(cmd: str) -> str:
    """Run a shell command. (Never permitted — present only to be denied.)"""
    return f"(should never run) {cmd}"


def rogue_specs() -> list[ToolSpec]:
    return [ToolSpec(name="shell_exec", description=shell_exec.__doc__ or "Run a shell command.",
                     parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
                     fn=shell_exec)]
