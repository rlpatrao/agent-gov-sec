"""
trace_ledger.py

Immutable append-only trace ledger in PostgreSQL.

Every agent action — LLM call, file read, decision, escalation — is recorded here.
Entries are hash-chained (each entry's hash includes the previous entry's hash)
making the ledger tamper-evident.

Schema (run once via infra/ledger_schema.sql):

    CREATE TABLE trace_ledger (
        id              BIGSERIAL PRIMARY KEY,
        run_id          TEXT        NOT NULL,
        module_id       TEXT        NOT NULL,
        agent_type      TEXT        NOT NULL,
        nhi_id          TEXT        NOT NULL,   -- which NHI performed this action
        action          TEXT        NOT NULL,   -- "llm_call" | "file_read" | "decision" | "escalation"
        input_summary   TEXT,                  -- first 200 chars of prompt (PII scrubbed)
        output_summary  TEXT,                  -- first 200 chars of response
        tokens_used     INTEGER     DEFAULT 0,
        attempt         INTEGER     NOT NULL,
        outcome         TEXT        NOT NULL,   -- "success" | "blocked" | "escalated" | "failed"
        entry_hash      TEXT        NOT NULL,   -- SHA-256 of this entry's content
        prev_hash       TEXT        NOT NULL,   -- hash of previous entry (chain)
        recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX idx_trace_run_id ON trace_ledger (run_id);
    CREATE INDEX idx_trace_module ON trace_ledger (run_id, module_id);
"""

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import asyncpg
    _ASYNCPG_AVAILABLE = True
except ImportError:
    _ASYNCPG_AVAILABLE = False
    logger.warning("asyncpg not installed — trace ledger will log to stdout only")


class TraceLedger:
    """
    Append-only, hash-chained trace ledger.

    One instance per Galaxy run.
    All agent actions write here — nothing is ever updated or deleted.

    Hash chain:
      entry_hash = SHA-256(run_id | module_id | agent_type | action | outcome | prev_hash)
      If any entry is modified, the chain breaks and the Compliance Auditor detects it.
    """

    _GENESIS_HASH = "genesis-0000000000000000000000000000000000000000000000000000000000000000"

    def __init__(self, run_id: str, db_pool=None):
        self._run_id = run_id
        self._pool = db_pool
        self._last_hash = self._GENESIS_HASH
        self._entry_count = 0

    @classmethod
    async def create(cls, run_id: str) -> "TraceLedger":
        """
        Factory — creates ledger and connects to PostgreSQL.
        Falls back to stdout-only mode if DB is unavailable (local dev).
        """
        instance = cls(run_id=run_id)

        if not _ASYNCPG_AVAILABLE:
            return instance

        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            logger.warning("trace_ledger.no_postgres_dsn — stdout mode only")
            return instance

        try:
            instance._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
            logger.info("trace_ledger.connected", extra={"run_id": run_id})
        except Exception as e:
            logger.error("trace_ledger.connection_failed", extra={"error": str(e)})

        return instance

    async def record(
        self,
        module_id: str,
        agent_type: str,
        nhi_id: str,
        action: str,
        outcome: str,
        input_summary: str = "",
        output_summary: str = "",
        tokens_used: int = 0,
        attempt: int = 1,
    ) -> str:
        """
        Write one immutable entry to the ledger.
        Returns the entry_hash for the caller to include in OTel spans.

        input_summary and output_summary must be PII-scrubbed before calling.
        Maximum 200 chars each — never the full prompt or response.
        """
        # Enforce summary length limits
        input_summary  = (input_summary  or "")[:200]
        output_summary = (output_summary or "")[:200]

        entry_hash = self._compute_hash(
            self._run_id,
            module_id,
            agent_type,
            action,
            outcome,
            str(attempt),
            self._last_hash,
        )

        entry = {
            "run_id":         self._run_id,
            "module_id":      module_id,
            "agent_type":     agent_type,
            "nhi_id":         nhi_id,
            "action":         action,
            "input_summary":  input_summary,
            "output_summary": output_summary,
            "tokens_used":    tokens_used,
            "attempt":        attempt,
            "outcome":        outcome,
            "entry_hash":     entry_hash,
            "prev_hash":      self._last_hash,
            "recorded_at":    datetime.now(timezone.utc).isoformat(),
        }

        if self._pool:
            await self._write_to_db(entry)
        else:
            # Local dev — stdout is the ledger
            logger.info("trace_ledger.entry", extra=entry)

        self._last_hash = entry_hash
        self._entry_count += 1

        return entry_hash

    async def _write_to_db(self, entry: dict) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO trace_ledger (
                        run_id, module_id, agent_type, nhi_id,
                        action, input_summary, output_summary,
                        tokens_used, attempt, outcome,
                        entry_hash, prev_hash, recorded_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7,
                        $8, $9, $10, $11, $12, $13
                    )
                """,
                    entry["run_id"],        entry["module_id"],
                    entry["agent_type"],    entry["nhi_id"],
                    entry["action"],        entry["input_summary"],
                    entry["output_summary"],entry["tokens_used"],
                    entry["attempt"],       entry["outcome"],
                    entry["entry_hash"],    entry["prev_hash"],
                    datetime.now(timezone.utc),
                )
        except Exception as e:
            # Ledger write failure is serious — log but don't crash the agent run
            logger.error(
                "trace_ledger.write_failed",
                extra={"error": str(e), "entry_hash": entry["entry_hash"]},
            )

    @staticmethod
    def _compute_hash(*parts: str) -> str:
        content = "|".join(parts)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def verify_chain(self) -> bool:
        """
        Verify the hash chain is intact for this run.
        Call from the Compliance Auditor agent.
        Returns True if chain is valid, False if tampered.
        """
        if not self._pool:
            logger.warning("trace_ledger.verify_skipped — no DB connection")
            return True

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM trace_ledger WHERE run_id=$1 ORDER BY id ASC",
                    self._run_id,
                )

            prev = self._GENESIS_HASH
            for row in rows:
                expected = self._compute_hash(
                    row["run_id"], row["module_id"], row["agent_type"],
                    row["action"], row["outcome"], str(row["attempt"]),
                    prev,
                )
                if expected != row["entry_hash"]:
                    logger.error(
                        "trace_ledger.chain_broken",
                        extra={"entry_id": row["id"], "run_id": self._run_id},
                    )
                    return False
                prev = row["entry_hash"]

            logger.info(
                "trace_ledger.chain_valid",
                extra={"run_id": self._run_id, "entries": len(rows)},
            )
            return True

        except Exception as e:
            logger.error("trace_ledger.verify_failed", extra={"error": str(e)})
            return False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
