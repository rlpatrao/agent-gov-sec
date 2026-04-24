"""
governance.middleware — single factory that builds the full MAF
middleware stack for any Galaxy agent.

Delegates the heavy lifting to agent_os.integrations.maf_adapter.
Our only additions are two AuditBackend implementations wired into
the GovernanceAuditLogger (Postgres hash chain + OTel to App Insights).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger, LoggingBackend
from agent_os.integrations.maf_adapter import create_governance_middleware

from governance.adapters.otel_audit_backend import OtelAuditBackend
from governance.adapters.postgres_audit_backend import PostgresHashChainBackend

logger = logging.getLogger(__name__)

_POLICY_DIR = Path(__file__).parent / "policies"


class _CompatAuditLogger(GovernanceAuditLogger):
    """Accepts both legacy kwargs-style and new ``log(entry)`` calls.

    agent_os_kernel 3.2.2 ships a maf_adapter that calls
    ``self.audit_log.log(event_type=..., agent_did=..., action=..., data=...,
    outcome=..., policy_decision=...)`` and expects an AuditEntry back —
    but the current ``GovernanceAuditLogger.log`` signature is
    ``log(self, entry: AuditEntry) -> None``. This shim bridges both.
    """

    def log(self, entry: AuditEntry | None = None, **kw: Any) -> AuditEntry:  # type: ignore[override]
        if entry is None:
            # Legacy kwargs path used by maf_adapter
            data = kw.pop("data", {}) or {}
            policy_decision = kw.pop("policy_decision", None)
            if policy_decision is not None:
                data = {**data, "policy_decision": policy_decision}
            agent_id = kw.pop("agent_did", kw.pop("agent_id", ""))
            entry = AuditEntry(
                event_type=kw.pop("event_type", ""),
                agent_id=agent_id,
                action=kw.pop("action", ""),
                decision=kw.pop("outcome", kw.pop("decision", "")),
                reason=kw.pop("reason", ""),
                latency_ms=float(kw.pop("latency_ms", 0.0) or 0.0),
                metadata={**data, **kw},
            )
        # maf_adapter reads start_entry.entry_id to correlate start/end pairs; add it.
        if not hasattr(entry, "entry_id"):
            object.__setattr__(entry, "entry_id", uuid.uuid4().hex)
        for backend in self._backends:
            try:
                backend.write(entry)
            except Exception as e:
                logger.error("audit_backend.write_failed", extra={"error": str(e), "backend": type(backend).__name__})
        return entry


async def build_governance_stack(
    agent_id: str,
    allowed_tools: Optional[list[str]] = None,
    denied_tools: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    enable_rogue_detection: bool = True,
) -> tuple[list, PostgresHashChainBackend, GovernanceAuditLogger]:
    """Return (middleware_list, pg_backend, audit_logger).

    Pass `middleware_list` directly to Agent(middleware=...).
    Call `await pg_backend.flush_async()` and `await pg_backend.close()` at end of run.
    `audit_logger` is returned so domain events (e.g. repo_traversal_complete)
    can be logged explicitly from the agent where useful.
    """
    audit = _CompatAuditLogger()
    audit.add_backend(LoggingBackend())                            # stdout fallback
    audit.add_backend(OtelAuditBackend())                          # App Insights
    pg_backend = await PostgresHashChainBackend.create(
        run_id=run_id or agent_id,
    )
    audit.add_backend(pg_backend)                                  # compliance archive

    middleware = create_governance_middleware(
        policy_directory=_POLICY_DIR,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        agent_id=agent_id,
        enable_rogue_detection=enable_rogue_detection,
        audit_log=audit,
    )

    logger.info(
        "governance.stack_built",
        extra={
            "agent_id": agent_id,
            "policy_dir": str(_POLICY_DIR),
            "middleware_count": len(middleware),
            "postgres_connected": pg_backend._pool is not None,
        },
    )
    return middleware, pg_backend, audit
