"""
governance.extensions.data_drift — Gap 3: data-access behavioral drift.

Our existing drift detection (``agent_sre.anomaly.RogueAgentDetector``) baselines
*tool-call* patterns (frequency z-score, Shannon action entropy, capability
deviation). This module is the **data-access** companion it doesn't cover: it
baselines *what data an agent reads* — volume (columns/rows), table sensitivity
touched, first-seen tables, and table-access entropy — and raises a risk score
with a quarantine recommendation, exactly as the action-level detector does.

It fixes the two acknowledged weaknesses of the action-level detector for the
data dimension:
  1. **data-level, not action-level** — features are about rows/tables/sensitivity.
  2. **persistent baselines** — baselines live behind a ``BaselineStore`` so they
     survive cold starts (the in-memory detector resets per process). A JSON-file
     store ships as the durable default; DynamoDB / Firestore / Postgres are the
     cloud adapters.

Feature-flagged off by default (``GALAXY_GAP_DATA_DRIFT``). Fed by the Gap-1
``DataAccessMediator`` (one ``record_access`` per authorize()).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Baseline persistence ──────────────────────────────────────────────────────

@runtime_checkable
class BaselineStore(Protocol):
    """Durable per-agent baseline storage. Cloud adapters: DynamoDB (AWS),
    Firestore/Bigtable (GCP), Postgres/Redis (Azure)."""

    def get(self, key: str) -> dict: ...
    def put(self, key: str, baseline: dict) -> None: ...


class InMemoryBaselineStore:
    """Ephemeral store — resets on process restart (the weakness we're fixing).
    Useful for tests and single-shot runs."""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    def get(self, key: str) -> dict:
        return self._data.get(key, {})

    def put(self, key: str, baseline: dict) -> None:
        self._data[key] = baseline


class JsonFileBaselineStore:
    """Durable default: one JSON file, survives cold starts. Path via arg or
    ``GALAXY_DRIFT_BASELINE_PATH`` (default ``./.galaxy/drift-baselines.json``)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        import os
        self._path = Path(path or os.environ.get("GALAXY_DRIFT_BASELINE_PATH", ".galaxy/drift-baselines.json"))

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text("utf-8"))
        except Exception as e:
            logger.warning("drift.baseline_load_failed", extra={"error": str(e)})
            return {}

    def get(self, key: str) -> dict:
        return self._load().get(key, {})

    def put(self, key: str, baseline: dict) -> None:
        data = self._load()
        data[key] = baseline
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data), "utf-8")


# ── Config + risk ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DriftConfig:
    min_samples: int = 5            # need a baseline before volume/sensitivity signals fire
    z_threshold: float = 3.0        # columns-read z-score for a volume spike
    window: int = 200               # cap on retained volume samples
    entropy_high: float = 2.5       # bits — high table-access entropy = scanning/erratic
    entropy_min_tables: int = 4     # only flag entropy once enough distinct tables seen
    denial_rate_threshold: float = 0.3
    quarantine_score: float = 0.6   # combined score at/above which to quarantine


@dataclass
class DataAccessRisk:
    agent_type: str
    score: float = 0.0
    level: str = "low"              # low | medium | high | critical
    signals: list[str] = field(default_factory=list)
    quarantine_recommended: bool = False

    def to_dict(self) -> dict:
        return {
            "agent_type": self.agent_type, "score": round(self.score, 3),
            "level": self.level, "signals": self.signals,
            "quarantine_recommended": self.quarantine_recommended,
        }


# Signal weights — sum, clamped to 1.0. sensitivity_escalation is the strongest
# data-exfiltration signal (reading above clearance), so reading a brand-new
# table above clearance (new_table + sensitivity_escalation = 0.60) reaches the
# quarantine threshold on its own.
_WEIGHTS = {
    "volume_spike": 0.35,
    "new_table": 0.20,
    "sensitivity_escalation": 0.40,
    "erratic_entropy": 0.20,
    "high_denial_rate": 0.25,
}


class DataAccessDriftDetector:
    """Baselines per-agent data-access patterns and scores drift per event."""

    def __init__(self, store: Optional[BaselineStore] = None, config: Optional[DriftConfig] = None) -> None:
        self._store = store or JsonFileBaselineStore()
        self._cfg = config or DriftConfig()

    def record_access(
        self,
        *,
        agent_type: str,
        dataset: str,
        table: str,
        columns_read: int,
        max_sensitivity: int,
        denied: bool = False,
    ) -> DataAccessRisk:
        """Score this access against the agent's baseline, then update + persist
        the baseline. Returns the risk for the caller to act on inline."""
        cfg = self._cfg
        b = self._store.get(agent_type) or {
            "samples": [], "tables": {}, "max_sensitivity_seen": 0,
            "access_count": 0, "denied_count": 0,
        }
        table_key = f"{dataset}.{table}"
        signals: list[str] = []

        # ── signals: this event vs the CURRENT baseline ──────────────────────
        if b["access_count"] >= cfg.min_samples and len(b["samples"]) >= 2:
            mean = statistics.fmean(b["samples"])
            std = statistics.pstdev(b["samples"]) or 1e-9
            if (columns_read - mean) / std > cfg.z_threshold:
                signals.append("volume_spike")

        if table_key not in b["tables"] and b["access_count"] >= cfg.min_samples:
            signals.append("new_table")    # first-seen table after a baseline exists

        if max_sensitivity > b["max_sensitivity_seen"] and b["access_count"] >= cfg.min_samples:
            signals.append("sensitivity_escalation")

        if denied:
            denial_rate = (b["denied_count"] + 1) / (b["access_count"] + 1)
            if denial_rate > cfg.denial_rate_threshold:
                signals.append("high_denial_rate")

        # ── update baseline ──────────────────────────────────────────────────
        b["samples"] = (b["samples"] + [columns_read])[-cfg.window:]
        b["tables"][table_key] = b["tables"].get(table_key, 0) + 1
        b["max_sensitivity_seen"] = max(b["max_sensitivity_seen"], max_sensitivity)
        b["access_count"] += 1
        if denied:
            b["denied_count"] += 1

        # entropy is computed on the UPDATED table distribution
        if len(b["tables"]) >= cfg.entropy_min_tables:
            if _shannon_entropy(b["tables"].values()) > cfg.entropy_high:
                signals.append("erratic_entropy")

        self._store.put(agent_type, b)

        return self._score(agent_type, signals)

    def baseline(self, agent_type: str) -> dict:
        return self._store.get(agent_type)

    def _score(self, agent_type: str, signals: list[str]) -> DataAccessRisk:
        score = min(1.0, sum(_WEIGHTS.get(s, 0.0) for s in signals))
        if score >= 0.8:
            level = "critical"
        elif score >= self._cfg.quarantine_score:
            level = "high"
        elif score >= 0.3:
            level = "medium"
        else:
            level = "low"
        risk = DataAccessRisk(
            agent_type=agent_type, score=score, level=level, signals=signals,
            quarantine_recommended=score >= self._cfg.quarantine_score,
        )
        if risk.quarantine_recommended:
            logger.warning("data_drift.quarantine_recommended", extra=risk.to_dict())
        return risk


def _shannon_entropy(counts) -> float:
    counts = [c for c in counts if c > 0]
    total = sum(counts)
    if total == 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts)
