"""
adapters.gcp.audit — BigQueryHashChainBackend (WS6).

An ``agent_os.audit_logger.AuditBackend`` that writes AuditEntry records to a
BigQuery table with the SHA-256 hash-chain preserved (the GCP analogue of
``adapters/azure/audit.PostgresHashChainBackend`` and
``adapters/aws/audit.DynamoDbHashChainBackend``). Each row carries
``entry_hash`` = SHA-256(run_id | module_id | agent_type | action | outcome |
attempt | prev_hash), so the chain is tamper-evident and ``verify_chain()`` can
re-derive it.

Buffering mirrors the other backends: ``write()`` is synchronous and queues;
``flush_async()`` (called by the agent runner at end of run) does the batched
BigQuery insert. With no ``google-cloud-bigquery`` or no dataset/table
configured, it runs in **stdout mode** — full chain logic active, no
persistence — so the platform never hard-fails on a missing ledger. The buffer
shape (``_buffer`` / ``_run_id`` / ``_agent_type`` / ``_decision_to_outcome``)
is identical to the other backends so the demo's chain verifier is portable.

Env:
  GALAXY_LEDGER_DATASET  — BigQuery dataset (default ``galaxy``)
  GALAXY_LEDGER_TABLE    — BigQuery table (default ``trace_ledger``)
  GOOGLE_CLOUD_PROJECT   — project id (required for live persistence)
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from agent_os.audit_logger import AuditEntry, AuditBackend

logger = logging.getLogger(__name__)

_GENESIS_HASH = "genesis-0000000000000000000000000000000000000000000000000000000000000000"


class BigQueryHashChainBackend(AuditBackend):
    """Synchronous AuditBackend that queues writes for a batched BigQuery insert."""

    def __init__(self, run_id: str, client=None, table_ref: str = "") -> None:
        self._run_id = run_id
        self._client = client
        self._table_ref = table_ref
        self._last_hash = _GENESIS_HASH
        self._entry_count = 0
        self._buffer: list[tuple[AuditEntry, str, str]] = []  # (entry, entry_hash, prev_hash)

    @classmethod
    async def create(cls, run_id: str, dataset: Optional[str] = None, table: Optional[str] = None) -> "BigQueryHashChainBackend":
        dataset = dataset or os.environ.get("GALAXY_LEDGER_DATASET", "galaxy")
        table = table or os.environ.get("GALAXY_LEDGER_TABLE", "trace_ledger")
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        instance = cls(run_id=run_id)
        if not project:
            logger.warning("bigquery_audit.stdout_mode", extra={"reason": "no GOOGLE_CLOUD_PROJECT"})
            return instance
        try:
            from google.cloud import bigquery
            instance._client = bigquery.Client(project=project)
            instance._table_ref = f"{project}.{dataset}.{table}"
            logger.info("bigquery_audit.connected", extra={"run_id": run_id, "table": instance._table_ref})
        except ImportError:
            logger.warning("bigquery_audit.stdout_mode", extra={"reason": "google-cloud-bigquery missing"})
        except Exception as e:
            logger.error("bigquery_audit.connection_failed", extra={"error": str(e)})
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
        logger.info(
            "bigquery_audit.queued",
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
        if self._client is None or not self._buffer:
            self._buffer.clear()
            return
        import asyncio

        def _insert_all():
            rows = []
            for seq, (entry, entry_hash, prev_hash) in enumerate(self._buffer):
                rows.append({
                    "run_id": self._run_id,
                    "entry_seq": seq,
                    "module_id": entry.metadata.get("module_id", "unknown"),
                    "agent_type": self._agent_type(entry),
                    "nhi_id": entry.metadata.get("nhi_id", entry.agent_id or "unknown"),
                    "action": entry.event_type or entry.action or "unknown",
                    "input_summary": (entry.metadata.get("input_summary") or "")[:200],
                    "output_summary": (entry.reason or "")[:200],
                    "tokens_used": int(entry.metadata.get("tokens_used", 0) or 0),
                    "attempt": int(entry.metadata.get("attempt", 1) or 1),
                    "outcome": self._decision_to_outcome(entry.decision),
                    "entry_hash": entry_hash,
                    "prev_hash": prev_hash,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                })
            errors = self._client.insert_rows_json(self._table_ref, rows)
            if errors:
                logger.error("bigquery_audit.insert_errors", extra={"errors": str(errors)[:300]})

        await asyncio.to_thread(_insert_all)
        self._buffer.clear()

    async def verify_chain(self) -> bool:
        if self._client is None:
            return True
        import asyncio

        def _query():
            sql = (
                f"SELECT * FROM `{self._table_ref}` "
                f"WHERE run_id = @run_id ORDER BY entry_seq ASC"
            )
            from google.cloud import bigquery
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", self._run_id),
            ])
            return list(self._client.query(sql, job_config=job_config).result())

        rows = await asyncio.to_thread(_query)
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
        # BigQuery client needs no explicit close; symmetric with the other
        # backends' close() so callers can await it uniformly.
        pass

    # ── Helpers (shared shape with the Postgres/DynamoDB/local backends) ──

    @staticmethod
    def _agent_type(entry: AuditEntry) -> str:
        aid = entry.metadata.get("agent_type") or entry.agent_id or "unknown"
        return aid.split("-", 1)[0].capitalize() if "-" in aid else aid

    @staticmethod
    def _decision_to_outcome(decision: str) -> str:
        mapping = {
            "allow": "success", "audit": "success",
            "deny": "blocked", "block": "failed", "": "success",
        }
        return mapping.get(decision, "success")


def _compute_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
