"""
cloud_adapters.local.audit — in-memory hash-chained AuditBackend (no cloud, no DB).

Same SHA-256 hash-chain contract as the Azure (Postgres) and AWS (DynamoDB)
backends — ``write``/``flush``/``flush_async``/``verify_chain`` plus the
``_agent_type`` / ``_decision_to_outcome`` helpers — but it persists nowhere.
For local/offline runs and the demo: the chain is built and verifiable in
memory, with zero cloud branding in the logs.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from agent_os.audit_logger import AuditEntry, AuditBackend

logger = logging.getLogger(__name__)

_GENESIS_HASH = "genesis-" + "0" * 64


class LocalHashChainBackend(AuditBackend):
    """In-memory hash-chain ledger. Buffers entries; never writes out."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._last_hash = _GENESIS_HASH
        self._entry_count = 0
        self._buffer: list[tuple[AuditEntry, str, str]] = []  # (entry, entry_hash, prev_hash)

    @classmethod
    async def create(cls, run_id: str) -> "LocalHashChainBackend":
        logger.info("local_audit.in_memory", extra={"run_id": run_id})
        return cls(run_id=run_id)

    def write(self, entry: AuditEntry) -> None:
        entry_hash = _compute_hash(
            self._run_id, entry.metadata.get("module_id", "unknown"),
            self._agent_type(entry), entry.event_type or entry.action or "unknown",
            self._decision_to_outcome(entry.decision), str(entry.metadata.get("attempt", 1)),
            self._last_hash,
        )
        self._buffer.append((entry, entry_hash, self._last_hash))
        self._last_hash = entry_hash
        self._entry_count += 1
        logger.info("local_audit.recorded", extra={"run_id": self._run_id,
                    "action": entry.event_type or entry.action, "decision": entry.decision})

    def flush(self) -> None:
        pass

    async def flush_async(self) -> None:
        pass  # in-memory — nothing to persist; buffer stays for verify/demo

    async def verify_chain(self) -> bool:
        prev = _GENESIS_HASH
        for entry, entry_hash, prev_hash in self._buffer:
            expected = _compute_hash(
                self._run_id, entry.metadata.get("module_id", "unknown"),
                self._agent_type(entry), entry.event_type or entry.action or "unknown",
                self._decision_to_outcome(entry.decision), str(entry.metadata.get("attempt", 1)), prev,
            )
            if expected != entry_hash or prev_hash != prev:
                return False
            prev = entry_hash
        return True

    async def close(self) -> None:
        pass

    @staticmethod
    def _agent_type(entry: AuditEntry) -> str:
        aid = entry.metadata.get("agent_type") or entry.agent_id or "unknown"
        return aid.split("-", 1)[0].capitalize() if "-" in aid else aid

    @staticmethod
    def _decision_to_outcome(decision: str) -> str:
        return {"allow": "success", "audit": "success", "deny": "blocked",
                "block": "failed", "": "success"}.get(decision, "success")


def _compute_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
