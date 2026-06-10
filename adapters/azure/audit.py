"""
PostgresHashChainBackend — an agent_os.audit_logger.AuditBackend that
writes AuditEntry records to the existing Galaxy trace_ledger schema
with the hash-chain preserved.

Bridge contract:
  AuditEntry field      →  trace_ledger column
  ───────────────────      ───────────────────────────────────────
  event_type             →  action
  agent_id               →  agent_type (first segment before '-', e.g. 'scanner-1' → 'Scanner')
  decision               →  outcome ('allow'→'success', 'deny'→'blocked', 'audit'→'success', 'block'→'failed')
  reason                 →  output_summary (first 200 chars)
  metadata['run_id']     →  run_id (REQUIRED — agent must set this in ctx)
  metadata['module_id']  →  module_id
  metadata['nhi_id']     →  nhi_id (falls back to agent_id)
  metadata['attempt']    →  attempt (int; default 1)
  metadata['input_summary']  →  input_summary (first 200 chars)
  metadata['tokens_used']    →  tokens_used
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from agent_os.audit_logger import AuditEntry, AuditBackend

logger = logging.getLogger(__name__)

try:
    import asyncpg  # type: ignore
    _ASYNCPG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ASYNCPG_AVAILABLE = False


_GENESIS_HASH = "genesis-0000000000000000000000000000000000000000000000000000000000000000"


class PostgresHashChainBackend(AuditBackend):
    """Synchronous AuditBackend that queues writes for an async worker."""

    def __init__(self, run_id: str, pool: Optional["asyncpg.Pool"] = None) -> None:
        self._run_id = run_id
        self._pool = pool
        self._last_hash = _GENESIS_HASH
        self._entry_count = 0
        # AuditBackend.write() is synchronous but we need to talk to an async pool.
        # We buffer entries and flush them on demand via flush_async() from the agent runner.
        self._buffer: list[tuple[AuditEntry, str, str]] = []   # (entry, entry_hash, prev_hash)

    @classmethod
    async def create(cls, run_id: str, dsn: Optional[str] = None) -> "PostgresHashChainBackend":
        import os
        dsn = dsn or os.environ.get("POSTGRES_DSN")
        instance = cls(run_id=run_id)
        if not _ASYNCPG_AVAILABLE or not dsn:
            logger.warning("postgres_audit.stdout_mode", extra={"reason": "no dsn or asyncpg missing"})
            return instance
        try:
            instance._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
            logger.info("postgres_audit.connected", extra={"run_id": run_id})
        except Exception as e:
            logger.error("postgres_audit.connection_failed", extra={"error": str(e)})
        return instance

    # ── AuditBackend protocol ─────────────────────────────────────────────

    def write(self, entry: AuditEntry) -> None:
        entry_hash = _compute_hash(
            self._run_id,
            entry.metadata.get("module_id", "unknown"),
            self._agent_type(entry),
            entry.event_type or entry.action or "unknown",
            self._decision_to_outcome(entry.decision),
            str(entry.metadata.get("attempt", 1)),
            self._last_hash,
        )
        self._buffer.append((entry, entry_hash, self._last_hash))
        self._last_hash = entry_hash
        self._entry_count += 1

        # Stdout audit trail, always on — gives immediate visibility even
        # before the async flush lands in Postgres.
        logger.info(
            "postgres_audit.queued",
            extra={
                "run_id": self._run_id,
                "action": entry.event_type or entry.action,
                "decision": entry.decision,
                "entry_hash": entry_hash,
            },
        )

    def flush(self) -> None:
        # Sync flush is a no-op; the async runner calls flush_async() at end of run.
        pass

    # ── Async flush + chain verify (called from the agent runner) ─────────

    async def flush_async(self) -> None:
        if not self._pool or not self._buffer:
            self._buffer.clear()
            return
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for entry, entry_hash, prev_hash in self._buffer:
                    await conn.execute(
                        """
                        INSERT INTO trace_ledger (
                            run_id, module_id, agent_type, nhi_id,
                            action, input_summary, output_summary,
                            tokens_used, attempt, outcome,
                            entry_hash, prev_hash, recorded_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                        """,
                        self._run_id,
                        entry.metadata.get("module_id", "unknown"),
                        self._agent_type(entry),
                        entry.metadata.get("nhi_id", entry.agent_id or "unknown"),
                        entry.event_type or entry.action or "unknown",
                        (entry.metadata.get("input_summary") or "")[:200],
                        (entry.reason or "")[:200],
                        int(entry.metadata.get("tokens_used", 0) or 0),
                        int(entry.metadata.get("attempt", 1) or 1),
                        self._decision_to_outcome(entry.decision),
                        entry_hash,
                        prev_hash,
                        datetime.now(timezone.utc),
                    )
        self._buffer.clear()

    async def verify_chain(self) -> bool:
        if not self._pool:
            return True
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trace_ledger WHERE run_id=$1 ORDER BY id ASC",
                self._run_id,
            )
        prev = _GENESIS_HASH
        for row in rows:
            expected = _compute_hash(
                row["run_id"], row["module_id"], row["agent_type"],
                row["action"], row["outcome"], str(row["attempt"]), prev,
            )
            if expected != row["entry_hash"]:
                return False
            prev = row["entry_hash"]
        return True

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _agent_type(entry: AuditEntry) -> str:
        """Scanner agents register as 'Scanner' even if their instance id is 'scanner-1'."""
        aid = entry.metadata.get("agent_type") or entry.agent_id or "unknown"
        # Convention: agent_id like "Scanner-run-123" → "Scanner"
        return aid.split("-", 1)[0].capitalize() if "-" in aid else aid

    @staticmethod
    def _decision_to_outcome(decision: str) -> str:
        # Audit entries use "allow|deny|audit|block"; ledger uses "success|blocked|escalated|failed"
        mapping = {
            "allow": "success",
            "audit": "success",
            "deny": "blocked",
            "block": "failed",
            "": "success",   # informational entries without a decision
        }
        return mapping.get(decision, "success")


def _compute_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
