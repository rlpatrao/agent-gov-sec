"""
governance.extensions.data_fgac — Gap 1: data-layer guardrails (FGAC).

Today governance stops at the IAM + tool boundary. This module adds a
**policy-enforcing data-access mediator** every agent read flows through, keyed
to the agent's NHI and a data-classification catalog. It decides — per dataset /
table / column — what the agent may read, and **masks** classified columns and
**filters** rows it isn't cleared for.

Two layers:
  - the **decision** (``authorize``) — pure, cloud-neutral, from the catalog/scope
    (``data_classification``). This is a declarative policy; it can be backed by
    ``agent_os.policies.PolicyEvaluator`` when a richer engine is wanted.
  - the **enforcement** (``enforce``) — the agnostic in-process enforcer applies
    the decision to rows the agent already fetched (masks/drops/filters). In
    production you push the decision down to the store so sensitive bytes never
    leave it: BigQuery column-level security + policy tags + dynamic data masking
    (GCP); Lake Formation FGAC + Glue catalog + Macie (AWS); Purview labels +
    SQL/Synapse CLS (Azure). Those are the ``DataAccessEnforcer`` adapters.

Feature-flagged off by default (``GALAXY_GAP_DATA_FGAC``). When a drift detector
is supplied, every authorize() is recorded for Gap 3 (data-access drift).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from governance.extensions.data_classification import (
    DataClassificationCatalog,
    Sensitivity,
)

logger = logging.getLogger(__name__)

_MASK = "***REDACTED***"


@dataclass(frozen=True)
class DataAccessDecision:
    """The mediator's per-request verdict."""
    agent_type: str
    dataset: str
    table: str
    allowed_columns: tuple[str, ...] = ()
    masked_columns: tuple[str, ...] = ()
    denied: bool = False                       # whole request out of scope
    reason: str = ""
    row_filter: dict[str, list] = field(default_factory=dict)
    nhi_id: str = ""                           # the authenticated principal this read is attributed to

    @property
    def permitted(self) -> bool:
        return not self.denied


@runtime_checkable
class DataAccessEnforcer(Protocol):
    """Pushes a decision down to the data store (BigQuery CLS, Lake Formation,
    Synapse CLS). The agnostic default enforces in-process on already-fetched
    rows; cloud adapters implement store-side pushdown."""

    def apply(self, decision: DataAccessDecision, rows: list[dict]) -> list[dict]: ...


class InProcessEnforcer:
    """Agnostic enforcer: masks/drops columns and filters rows in Python.

    Cloud-neutral and always correct, but reads the bytes first — use a
    store-side adapter in production for true non-exfiltration.
    """

    def apply(self, decision: DataAccessDecision, rows: list[dict]) -> list[dict]:
        if decision.denied:
            return []
        allowed = set(decision.allowed_columns)
        masked = set(decision.masked_columns)
        out: list[dict] = []
        for row in rows:
            if not _passes_row_filter(row, decision.row_filter):
                continue
            projected = {}
            for col, val in row.items():
                if col in masked:
                    projected[col] = _MASK
                elif col in allowed:
                    projected[col] = val
                # columns neither allowed nor masked are dropped (deny-by-omission)
            out.append(projected)
        return out


def _passes_row_filter(row: dict, row_filter: dict[str, list]) -> bool:
    for col, allowed_values in row_filter.items():
        if col in row and row[col] not in allowed_values:
            return False
    return True


class DataAccessMediator:
    """The decision point for agent data reads. Keyed to the agent's NHI scope
    and the classification catalog."""

    def __init__(
        self,
        catalog: Optional[DataClassificationCatalog] = None,
        enforcer: Optional[DataAccessEnforcer] = None,
        drift_detector: Optional[Any] = None,   # governance.extensions.data_drift.DataAccessDriftDetector
    ) -> None:
        self._catalog = catalog or DataClassificationCatalog.load()
        self._enforcer = enforcer or InProcessEnforcer()
        self._drift = drift_detector

    def authorize_for_identity(
        self,
        *,
        identity: Any,                          # core.nhi_registry.AgentIdentity (duck-typed: .agent_type, .client_id)
        dataset: str,
        table: str,
        columns: list[str],
    ) -> DataAccessDecision:
        """Authorize using the agent's **NHI identity** (from ``NHIRegistry.get``).
        The scope is keyed on ``identity.agent_type`` — the same key the NHI
        registry uses — and the decision is attributed to ``identity.client_id``
        (the ``nhi_id`` carried into the audit ledger). This is the binding
        between *which principal* and *which data*."""
        return self.authorize(
            agent_type=getattr(identity, "agent_type", "unknown"),
            dataset=dataset, table=table, columns=columns,
            nhi_id=getattr(identity, "client_id", "") or str(identity),
        )

    def authorize(
        self,
        *,
        agent_type: str,
        dataset: str,
        table: str,
        columns: list[str],
        nhi_id: str = "",
    ) -> DataAccessDecision:
        """Decide which of ``columns`` the agent may read (allow), must see
        masked (mask), or cannot touch (drop), and whether the whole request is
        out of scope (deny). ``nhi_id`` is the authenticated principal the read
        is attributed to (pass it from the agent's NHI; see
        ``authorize_for_identity``)."""
        scope = self._catalog.scope_for(agent_type)

        if dataset not in scope.allowed_datasets:
            decision = DataAccessDecision(
                agent_type=agent_type, dataset=dataset, table=table, nhi_id=nhi_id,
                denied=True, reason=f"dataset '{dataset}' not in agent scope",
            )
            self._record(decision, columns)
            return decision

        allowed, masked = [], []
        for col in columns:
            sens = self._catalog.sensitivity_of(dataset, table, col)
            if col in scope.masked_columns or sens > scope.max_sensitivity:
                masked.append(col)          # within reach but classified above clearance → mask
            else:
                allowed.append(col)

        decision = DataAccessDecision(
            agent_type=agent_type, dataset=dataset, table=table, nhi_id=nhi_id,
            allowed_columns=tuple(allowed), masked_columns=tuple(masked),
            denied=False,
            reason="ok" if not masked else f"masked {len(masked)} classified column(s)",
            row_filter=dict(scope.row_filter),
        )
        self._record(decision, columns)
        return decision

    def read(
        self,
        *,
        agent_type: str,
        dataset: str,
        table: str,
        columns: list[str],
        rows: list[dict],
        nhi_id: str = "",
    ) -> tuple[DataAccessDecision, list[dict]]:
        """Authorize + enforce in one call: returns (decision, masked/filtered rows)."""
        decision = self.authorize(
            agent_type=agent_type, dataset=dataset, table=table, columns=columns, nhi_id=nhi_id,
        )
        return decision, self._enforcer.apply(decision, rows)

    def _record(self, decision: DataAccessDecision, requested_columns: list[str]) -> None:
        # Feed Gap-3 data-access drift, if wired.
        if self._drift is None:
            return
        try:
            max_sens = max(
                (self._catalog.sensitivity_of(decision.dataset, decision.table, c) for c in requested_columns),
                default=Sensitivity.PUBLIC,
            )
            self._drift.record_access(
                agent_type=decision.agent_type,
                dataset=decision.dataset,
                table=decision.table,
                columns_read=len(decision.allowed_columns),
                max_sensitivity=int(max_sens),
                denied=decision.denied,
            )
        except Exception as e:  # drift must never break a data read
            logger.warning("data_fgac.drift_record_failed", extra={"error": str(e)})
