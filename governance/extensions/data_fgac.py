"""
governance.extensions.data_fgac — Gap 1: data-layer guardrails (FGAC).

Governance otherwise stops at the IAM + tool boundary. This module adds a
**policy-enforcing data-access mediator** every agent read flows through, keyed
to the agent's NHI. It decides — per dataset / table / column — what the agent
may read, **masks** classified columns it isn't cleared for, and **filters**
rows it isn't scoped to.

Split of responsibility (post-Casbin/MSGK review):
  - **Decision + classification = MSGK.** The mediator delegates the per-column
    allow/deny to ``agent_os.policies.data_classification.DataAccessEvaluator``
    (ABAC: classification ≤ clearance, category allow/deny, geography) — or,
    when a Cedar authorizer is injected, to MSGK's Cedar backend. We do **not**
    re-implement the decision.
  - **Enforcement = ours.** ``InProcessEnforcer`` masks/drops columns and filters
    rows post-fetch; cloud adapters (``adapters/aws/data_fgac`` Lake Formation /
    Athena pushdown) enforce store-side so sensitive bytes never leave the store.

Feature-flagged off by default (``GALAXY_GAP_DATA_FGAC``). When a drift detector
is supplied, every authorize() is recorded for Gap 3 (data-access drift).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from governance.extensions.data_classification import (
    DataAccessEvaluator,
    DataClassificationCatalog,
)

logger = logging.getLogger(__name__)

_MASK = "***REDACTED***"


@dataclass(frozen=True)
class DataAccessDecision:
    """The mediator's per-request **enforcement verdict** (distinct from MSGK's
    per-label ``DataAccessDecision``, which this composes)."""
    agent_type: str
    dataset: str
    table: str
    allowed_columns: tuple[str, ...] = ()
    masked_columns: tuple[str, ...] = ()
    denied: bool = False                       # whole request out of scope (no policy)
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
    store-side adapter (e.g. ``adapters/aws/data_fgac``) in production for true
    non-exfiltration.
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
    """Decision point for agent data reads. Keyed to the agent's NHI; decision
    delegated to MSGK (native ABAC evaluator, or an injected Cedar authorizer)."""

    def __init__(
        self,
        catalog: Optional[DataClassificationCatalog] = None,
        enforcer: Optional[DataAccessEnforcer] = None,
        drift_detector: Optional[Any] = None,   # governance.extensions.data_drift.DataAccessDriftDetector
        authorizer: Optional[Any] = None,        # governance.extensions.policy_engine.CedarAuthorizer (standards-based)
    ) -> None:
        self._catalog = catalog or DataClassificationCatalog.load()
        self._enforcer = enforcer or InProcessEnforcer()
        self._drift = drift_detector
        self._authorizer = authorizer

    def authorize_for_identity(self, *, identity: Any, dataset: str, table: str, columns: list[str]) -> DataAccessDecision:
        """Authorize using the agent's **NHI identity** (from ``NHIRegistry.get``):
        scope keyed on ``identity.agent_type`` (the NHI registry's key), decision
        attributed to ``identity.client_id`` (the ``nhi_id`` in the audit ledger)."""
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
        """Per-column allow/mask, or whole-request deny (no policy). Decision is
        MSGK's; masking/filtering is enforced downstream."""
        policies = self._catalog.policies_for(agent_type)
        if not policies and self._authorizer is None:
            decision = DataAccessDecision(
                agent_type=agent_type, dataset=dataset, table=table, nhi_id=nhi_id,
                denied=True, reason=f"no ABAC policy for agent '{agent_type}'",
            )
            self._record(decision, columns)
            return decision

        evaluator = DataAccessEvaluator(policies) if policies else None
        enforcement = self._catalog.enforcement_for(agent_type)
        allowed, masked = [], []
        for col in columns:
            label = self._catalog.label_for(dataset, table, col)
            if self._authorizer is not None:
                permitted = self._authorizer.authorize_data(
                    agent_type=agent_type, dataset=dataset, table=table, column=col, label=label,
                )
            else:
                permitted = evaluator.evaluate(agent_id=nhi_id or agent_type, data_label=label).allowed
            if not permitted or col in enforcement.masked_columns:
                masked.append(col)             # denied-by-policy OR explicitly masked → redacted column
            else:
                allowed.append(col)

        decision = DataAccessDecision(
            agent_type=agent_type, dataset=dataset, table=table, nhi_id=nhi_id,
            allowed_columns=tuple(allowed), masked_columns=tuple(masked), denied=False,
            reason="ok" if not masked else f"masked {len(masked)} column(s) above clearance/scope",
            row_filter=dict(enforcement.row_filter),
        )
        self._record(decision, columns)
        return decision

    def read(
        self, *, agent_type: str, dataset: str, table: str, columns: list[str], rows: list[dict], nhi_id: str = "",
    ) -> tuple[DataAccessDecision, list[dict]]:
        """Authorize + enforce in one call: returns (decision, masked/filtered rows)."""
        decision = self.authorize(agent_type=agent_type, dataset=dataset, table=table, columns=columns, nhi_id=nhi_id)
        return decision, self._enforcer.apply(decision, rows)

    def _record(self, decision: DataAccessDecision, requested_columns: list[str]) -> None:
        if self._drift is None:
            return
        try:
            max_sens = max(
                (int(self._catalog.label_for(decision.dataset, decision.table, c).classification) for c in requested_columns),
                default=0,
            )
            self._drift.record_access(
                agent_type=decision.agent_type, dataset=decision.dataset, table=decision.table,
                columns_read=len(decision.allowed_columns), max_sensitivity=max_sens, denied=decision.denied,
            )
        except Exception as e:  # drift must never break a data read
            logger.warning("data_fgac.drift_record_failed", extra={"error": str(e)})
