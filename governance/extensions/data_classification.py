"""
governance.extensions.data_classification — schema classification catalog +
per-agent ABAC policies, built on **MSGK's** ``agent_os.policies.data_classification``.

We do **not** re-implement classification or the ABAC decision — those are MSGK's
``DataClassification`` (PUBLIC<INTERNAL<CONFIDENTIAL<RESTRICTED<TOP_SECRET),
``DataLabel``, ``ABACPolicy`` and ``DataAccessEvaluator`` (most-restrictive-wins,
with ``classify_text`` / ``detect_pii/phi/pci`` for unstructured content). This
module is just the **deployment config**: a YAML map of which schema column
carries which label, and each agent's ABAC policy — turned into MSGK objects.

The decision is MSGK's; the *enforcement* (row/column masking, cloud pushdown)
lives in ``data_fgac`` and ``cloud_adapters/<cloud>/data_fgac`` and is genuinely ours.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# MSGK ABAC primitives — the classification + decision logic is upstream.
from agent_os.policies.data_classification import (  # noqa: F401  (re-export)
    ABACPolicy,
    DataAccessEvaluator,
    DataClassification,
    DataLabel,
    classify_text,
    detect_pii,
)

logger = logging.getLogger(__name__)


def _parse_classification(value) -> DataClassification:
    try:
        return DataClassification[str(value).strip().upper()]
    except KeyError:
        logger.warning("classification.unknown_label", extra={"value": value})
        return DataClassification.RESTRICTED  # fail-closed


@dataclass(frozen=True)
class EnforcementScope:
    """Enforcement-only directives (ours, not MSGK's decision): always-mask
    overrides + row filter. MSGK decides *whether* an agent may read a label;
    these say *how* to enforce on the rows/columns it gets back."""
    masked_columns: frozenset[str] = frozenset()
    row_filter: dict[str, list] = field(default_factory=dict)


@dataclass(frozen=True)
class DataClassificationCatalog:
    """Schema column → MSGK ``DataLabel`` + agent → MSGK ``ABACPolicy``."""
    _labels: dict[str, dict[str, dict[str, DataLabel]]] = field(default_factory=dict)
    _policies: dict[str, ABACPolicy] = field(default_factory=dict)
    _enforcement: dict[str, EnforcementScope] = field(default_factory=dict)

    # ── lookups ───────────────────────────────────────────────────────────
    def label_for(self, dataset: str, table: str, column: str) -> DataLabel:
        """MSGK ``DataLabel`` for a column; unclassified columns fail-closed to
        a RESTRICTED label."""
        try:
            return self._labels[dataset][table][column]
        except KeyError:
            return DataLabel(classification=DataClassification.RESTRICTED, categories=[])

    def policies_for(self, agent_type: str) -> list[ABACPolicy]:
        """MSGK ``ABACPolicy`` list for an agent (empty → no access)."""
        p = self._policies.get(agent_type)
        return [p] if p else []

    def enforcement_for(self, agent_type: str) -> EnforcementScope:
        return self._enforcement.get(agent_type, EnforcementScope())

    def has_agent(self, agent_type: str) -> bool:
        return agent_type in self._policies

    # ── loading ───────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict) -> "DataClassificationCatalog":
        labels: dict[str, dict[str, dict[str, DataLabel]]] = {}
        for ds, dsv in (data.get("classification", {}).get("datasets", {}) or {}).items():
            labels[ds] = {}
            for tbl, tblv in (dsv.get("tables", {}) or {}).items():
                labels[ds][tbl] = {}
                for col, spec in (tblv.get("columns", {}) or {}).items():
                    spec = spec or {}
                    labels[ds][tbl][col] = DataLabel(
                        classification=_parse_classification(spec.get("classification", "RESTRICTED")),
                        categories=list(spec.get("categories", []) or []),
                        geography=spec.get("geography", "") or "",
                    )
        policies: dict[str, ABACPolicy] = {}
        for agent, sv in (data.get("agent_policies", {}) or {}).items():
            sv = sv or {}
            policies[agent] = ABACPolicy(
                agent_id=agent,
                allowed_classifications=[_parse_classification(c) for c in (sv.get("allowed_classifications", []) or [])],
                allowed_categories=list(sv.get("allowed_categories", []) or []),
                denied_categories=list(sv.get("denied_categories", []) or []),
                required_geography=sv.get("required_geography") or None,
                max_classification=_parse_classification(sv.get("max_classification", "PUBLIC")),
            )
        enforcement: dict[str, EnforcementScope] = {}
        for agent, ev in (data.get("enforcement", {}) or {}).items():
            ev = ev or {}
            enforcement[agent] = EnforcementScope(
                masked_columns=frozenset(ev.get("masked_columns", []) or []),
                row_filter=dict(ev.get("row_filter", {}) or {}),
            )
        return cls(_labels=labels, _policies=policies, _enforcement=enforcement)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "DataClassificationCatalog":
        """Resolution: explicit ``path`` → ``GALAXY_DATA_CLASSIFICATION_PATH`` env
        (ops-supplied catalog) → bundled example."""
        if path is None:
            env_path = os.environ.get("GALAXY_DATA_CLASSIFICATION_PATH")
            path = Path(env_path) if env_path else (Path(__file__).parent / "configs" / "data-classification.example.yaml")
        if not path.exists():
            logger.warning("classification.catalog_missing", extra={"path": str(path)})
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})
