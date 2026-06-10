"""
adapters.aws.audit — DynamoDbHashChainBackend.

An ``agent_os.audit_logger.AuditBackend`` that writes AuditEntry records to a
DynamoDB table with the SHA-256 hash-chain preserved (the AWS analogue of
``adapters/azure/audit.PostgresHashChainBackend``). Each item carries
``entry_hash`` = SHA-256(run_id | module_id | agent_type | action | outcome |
attempt | prev_hash), so the chain is tamper-evident and ``verify_chain()`` can
re-derive it.

Buffering mirrors the Postgres backend: ``write()`` is synchronous and queues;
``flush_async()`` (called by the agent runner at end of run) does the batched
DynamoDB write. With no ``boto3`` or no table configured, it runs in **stdout
mode** — full chain logic active, no persistence — so the platform never hard-
fails on a missing ledger.

Env:
  GALAXY_LEDGER_TABLE  — DynamoDB table name (default ``galaxy-trace-ledger``)
  AWS_REGION           — region (default ``us-east-1``)
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


class DynamoDbHashChainBackend(AuditBackend):
    """Synchronous AuditBackend that queues writes for a batched DynamoDB flush."""

    def __init__(self, run_id: str, table=None, table_name: str = "galaxy-trace-ledger") -> None:
        self._run_id = run_id
        self._table = table
        self._table_name = table_name
        self._last_hash = _GENESIS_HASH
        self._entry_count = 0
        self._buffer: list[tuple[AuditEntry, str, str]] = []  # (entry, entry_hash, prev_hash)

    @classmethod
    async def create(cls, run_id: str, table_name: Optional[str] = None) -> "DynamoDbHashChainBackend":
        table_name = table_name or os.environ.get("GALAXY_LEDGER_TABLE", "galaxy-trace-ledger")
        instance = cls(run_id=run_id, table_name=table_name)
        try:
            import boto3
            region = os.environ.get("AWS_REGION", "us-east-1")
            instance._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
            logger.info("dynamodb_audit.connected", extra={"run_id": run_id, "table": table_name})
        except ImportError:
            logger.warning("dynamodb_audit.stdout_mode", extra={"reason": "boto3 missing"})
        except Exception as e:
            logger.error("dynamodb_audit.connection_failed", extra={"error": str(e)})
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
            "dynamodb_audit.queued",
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
        if self._table is None or not self._buffer:
            self._buffer.clear()
            return
        # DynamoDB batch_writer is sync boto3; run it in a thread to avoid blocking the loop.
        import asyncio

        def _write_all():
            with self._table.batch_writer() as batch:
                for entry, entry_hash, prev_hash in self._buffer:
                    batch.put_item(
                        Item={
                            "run_id": self._run_id,
                            "entry_seq": int(self._buffer.index((entry, entry_hash, prev_hash))),
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
                        }
                    )

        await asyncio.to_thread(_write_all)
        self._buffer.clear()

    async def verify_chain(self) -> bool:
        if self._table is None:
            return True
        import asyncio

        def _scan():
            resp = self._table.query(
                KeyConditionExpression="run_id = :r",
                ExpressionAttributeValues={":r": self._run_id},
                ScanIndexForward=True,
            )
            return resp.get("Items", [])

        rows = await asyncio.to_thread(_scan)
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
        # boto3 resource/clients need no explicit close; symmetric with the
        # Postgres backend's close() so callers can await it uniformly.
        pass

    # ── Helpers (shared shape with the Postgres backend) ──────────────────

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
