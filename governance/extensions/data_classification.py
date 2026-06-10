"""
governance.extensions.data_classification — the data-classification catalog
and per-agent data scope (Gap 1 support).

Two declarative inputs, loaded from one YAML file:

  classification:  source → dataset → table → column → sensitivity label
  agent_scopes:    agent-type → {allowed_datasets, max_sensitivity, masked_columns,
                                  row_filter}

Sensitivity is ordered (PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED) so the
mediator can compare a column's label against an agent's clearance. This module
is pure data + lookups — no cloud SDK, no enforcement; the mediator
(``data_fgac``) turns it into allow/mask/deny decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


class Sensitivity(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

    @classmethod
    def parse(cls, value: str) -> "Sensitivity":
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            logger.warning("classification.unknown_sensitivity", extra={"value": value})
            return cls.RESTRICTED  # fail-closed: unknown labels are treated as most sensitive


@dataclass(frozen=True)
class AgentDataScope:
    """What one agent type may read."""
    allowed_datasets: frozenset[str] = frozenset()
    max_sensitivity: Sensitivity = Sensitivity.PUBLIC
    masked_columns: frozenset[str] = frozenset()
    row_filter: dict[str, list] = field(default_factory=dict)   # column -> allowed values


@dataclass(frozen=True)
class DataClassificationCatalog:
    """Column-level sensitivity labels + per-agent scopes."""
    # dataset -> table -> column -> Sensitivity
    _labels: dict[str, dict[str, dict[str, Sensitivity]]] = field(default_factory=dict)
    _scopes: dict[str, AgentDataScope] = field(default_factory=dict)

    # ── lookups ───────────────────────────────────────────────────────────
    def sensitivity_of(self, dataset: str, table: str, column: str) -> Sensitivity:
        """Label for a column; unclassified columns fail-closed to RESTRICTED."""
        try:
            return self._labels[dataset][table][column]
        except KeyError:
            return Sensitivity.RESTRICTED

    def scope_for(self, agent_type: str) -> AgentDataScope:
        """Scope for an agent; unknown agents get the empty (deny-all) scope."""
        return self._scopes.get(agent_type, AgentDataScope())

    def has_scope(self, agent_type: str) -> bool:
        return agent_type in self._scopes

    # ── loading ───────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict) -> "DataClassificationCatalog":
        labels: dict[str, dict[str, dict[str, Sensitivity]]] = {}
        for ds, dsv in (data.get("classification", {}).get("datasets", {}) or {}).items():
            labels[ds] = {}
            for tbl, tblv in (dsv.get("tables", {}) or {}).items():
                labels[ds][tbl] = {
                    col: Sensitivity.parse(lbl)
                    for col, lbl in (tblv.get("columns", {}) or {}).items()
                }
        scopes: dict[str, AgentDataScope] = {}
        for agent, sv in (data.get("agent_scopes", {}) or {}).items():
            scopes[agent] = AgentDataScope(
                allowed_datasets=frozenset(sv.get("allowed_datasets", []) or []),
                max_sensitivity=Sensitivity.parse(sv.get("max_sensitivity", "PUBLIC")),
                masked_columns=frozenset(sv.get("masked_columns", []) or []),
                row_filter=dict(sv.get("row_filter", {}) or {}),
            )
        return cls(_labels=labels, _scopes=scopes)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "DataClassificationCatalog":
        """Resolution order: explicit ``path`` arg → ``GALAXY_DATA_CLASSIFICATION_PATH``
        env var (the ops-supplied catalog) → the bundled example. Ops point the
        env var at their own catalog; no code change needed."""
        import os
        if path is None:
            env_path = os.environ.get("GALAXY_DATA_CLASSIFICATION_PATH")
            path = Path(env_path) if env_path else (Path(__file__).parent / "configs" / "data-classification.example.yaml")
        if not path.exists():
            logger.warning("classification.catalog_missing", extra={"path": str(path)})
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})
