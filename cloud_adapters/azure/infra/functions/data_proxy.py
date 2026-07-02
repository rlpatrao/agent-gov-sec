"""
cloud_adapters/azure/infra/functions/data_proxy.py — the data-access chokepoint.

The Azure counterpart of ``cloud_adapters/aws/infra/lambda/data_proxy.py``. FGAC
enforced out-of-process (mechanism 4, full-out-of-process; see
docs/shared/governance-authority.md). Data reads are the one class of governed
event that never traverses the LLM proxy, so moving FGAC out of the agent
requires its own chokepoint. The agent's data tool calls this function with only
``(agent_type, dataset, table, columns)`` — it never sends rows. The function, in
its own identity (a Managed Identity that is the sole principal with read access
to the store), reads the rows itself, runs the ABAC decision + masking/row-filter
through the in-process ``DataAccessMediator`` engine, and returns the enforced
result. An agent cannot bypass the mask by reading the store directly, because
Entra/RBAC denies the agent that access; only this identity has it.

Fail-closed: an agent type with no ABAC policy in the classification catalog
resolves to deny-all (the mediator's existing behaviour), and an identity absent
from the policy registry is rejected before any read.

The row source is pluggable (``_read_source``): the demo reads the bundled
fixtures; a real deployment reads Azure SQL / Synapse with the function's own
Managed Identity (the agent has no direct access), optionally pushed down via
``cloud_adapters.azure.data_fgac.AzureSqlFgacEnforcer``.
"""

from __future__ import annotations

import json
import os

from governance.shared.policy_registry import load_registry, policy_for

_catalog = None
_mediator = None
_registry_cache = None


def _log(event, **fields):
    print(json.dumps({"event": event, **fields}))


def _mediator_engine():
    """Build (once) the FGAC mediator that owns the classification catalog."""
    global _catalog, _mediator
    if _mediator is None:
        from governance.shared.enforcement.data_classification import DataClassificationCatalog
        from governance.shared.enforcement.data_fgac import DataAccessMediator
        path = os.environ.get("GOV_DATA_CLASSIFICATION_PATH") or _default_catalog_path()
        _catalog = DataClassificationCatalog.load(path)
        _mediator = DataAccessMediator(catalog=_catalog)
    return _mediator


def _default_catalog_path():
    from pathlib import Path
    return (Path(__file__).resolve().parents[4]
            / "governance" / "shared" / "enforcement" / "configs" / "data-classification.example.yaml")


def _registry():
    global _registry_cache
    if _registry_cache is None:
        raw = os.environ.get("GOV_POLICY_REGISTRY")
        if not raw:
            path = os.environ.get("GOV_POLICY_REGISTRY_PATH")
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
        _registry_cache = load_registry(raw) if raw else {}
    return _registry_cache


def _read_source(dataset, table):
    """Read rows from the store the function owns. Demo: bundled fixtures. Real
    deployment: replace with an Azure SQL / Synapse read under the function's own
    Managed Identity (the agent has no direct access)."""
    from payload_agents._lib import demo_data
    return demo_data.rows_for(dataset, table)


def enforce_data(body: dict, headers: dict) -> tuple[int, dict]:
    """Pure decision function. Returns (status_code, payload)."""
    headers = {(k or "").lower(): v for k, v in (headers or {}).items()}
    agent_type = headers.get("x-agent-type")
    nhi_id = headers.get("x-nhi-id")

    # Identity must be known to the registry, else deny before any read.
    if policy_for(_registry(), agent_type) is None and _registry():
        _log("data_proxy.policy_denied", agent=agent_type, nhi=nhi_id)
        return 403, {"error": "no_governance_policy", "agent_type": agent_type}

    dataset, table = body.get("dataset"), body.get("table")
    columns = body.get("columns") or []
    if not agent_type or not dataset or not table:
        return 400, {"error": "missing agent_type/dataset/table"}

    mediator = _mediator_engine()
    rows = _read_source(dataset, table)  # function reads; agent never supplies rows
    decision, enforced = mediator.read(
        agent_type=agent_type, dataset=dataset, table=table,
        columns=list(columns), rows=rows, nhi_id=nhi_id or agent_type,
    )
    _log("data_proxy.read", agent=agent_type, dataset=dataset, table=table,
         denied=decision.denied, masked=list(decision.masked_columns),
         allowed=list(decision.allowed_columns), rows=len(enforced))

    if decision.denied:
        return 403, {"error": "data_access_denied", "dataset": dataset, "table": table}

    return 200, {
        "denied": decision.denied,
        "masked_columns": list(decision.masked_columns),
        "allowed_columns": list(decision.allowed_columns),
        "rows": enforced,
    }
