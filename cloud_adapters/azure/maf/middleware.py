"""
cloud_adapters.azure.maf.middleware — the MAF middleware-stack assembly.

This is the framework-axis glue of the Azure bundle: it composes MSGK's
``agent_os`` governance primitives into a Microsoft Agent Framework middleware
list. The agnostic governance pieces it draws on stay in ``governance/``:
the policy YAML set, the prompt-injection config, and the OTel audit backend.
The Azure-specific pieces are the MAF guard wrappers (this package) and the
Postgres hash-chain ledger (``cloud_adapters.azure.audit``).

Delegates the heavy lifting to ``agent_os.integrations.maf_adapter``.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional

import governance
from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger, LoggingBackend
from agent_os.context_budget import ContextScheduler
from agent_os.integrations.maf_adapter import create_governance_middleware
from agent_os.prompt_injection import ThreatLevel

from governance.adapters.otel_audit_backend import OtelAuditBackend
from cloud_adapters.azure.audit import PostgresHashChainBackend
from cloud_adapters.azure.maf.guards.context_budget import ContextBudgetGuardMiddleware
from cloud_adapters.azure.maf.guards.credential_redactor import CredentialRedactorGuardMiddleware
from cloud_adapters.azure.maf.guards.prompt_injection import PromptInjectionGuardMiddleware

logger = logging.getLogger(__name__)

# Policy + config sets are agnostic governance assets and stay in governance/.
_GOVERNANCE_DIR = Path(governance.__file__).parent
_POLICY_DIR = _GOVERNANCE_DIR / "policies"
_CONFIG_DIR = _GOVERNANCE_DIR / "configs"
_PROMPT_INJECTION_CONFIG = _CONFIG_DIR / "prompt-injection.yaml"


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


_THRESHOLD_MAP: dict[str, ThreatLevel] = {
    "medium":   ThreatLevel.MEDIUM,
    "high":     ThreatLevel.HIGH,
    "critical": ThreatLevel.CRITICAL,
}


async def build_governance_stack(
    agent_id: str,
    allowed_tools: Optional[list[str]] = None,
    denied_tools: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    enable_rogue_detection: bool = True,
    enable_prompt_injection_guard: bool = True,
    enable_credential_redactor: bool = True,
    credential_mode: str = "redact",                # "redact" | "deny"
    enable_context_budget: bool = True,
    context_budget_total_tokens: int = 8000,
    prompt_injection_block_threshold: str = "medium",
) -> tuple[list, PostgresHashChainBackend, GovernanceAuditLogger]:
    """Return (middleware_list, pg_backend, audit_logger).

    Pass `middleware_list` directly to Agent(middleware=...).
    Call `await pg_backend.flush_async()` and `await pg_backend.close()` at end of run.
    `audit_logger` is returned so domain events (e.g. repo_traversal_complete)
    can be logged explicitly from the agent where useful.

    The middleware list is ordered to fail fast on cheap checks first:
      1. PromptInjectionGuardMiddleware    (literal-string + heuristics, no LLM)
      2. CredentialRedactorGuardMiddleware (regex scan)
      3. ContextBudgetGuardMiddleware      (token allocate, no LLM)
      4. AuditTrailMiddleware              (from create_governance_middleware)
      5. GovernancePolicyMiddleware        (from create_governance_middleware — YAML rules)
      6. CapabilityGuardMiddleware         (from create_governance_middleware, if tools)
      7. RogueDetectionMiddleware          (from create_governance_middleware)
    """
    audit = _CompatAuditLogger()
    audit.add_backend(LoggingBackend())                            # stdout fallback
    audit.add_backend(OtelAuditBackend())                          # App Insights
    pg_backend = await PostgresHashChainBackend.create(
        run_id=run_id or agent_id,
    )
    audit.add_backend(pg_backend)                                  # compliance archive

    pre_middleware: list = []
    if enable_prompt_injection_guard:
        pre_middleware.append(PromptInjectionGuardMiddleware(
            agent_id=agent_id,
            audit_log=audit,
            config_path=_PROMPT_INJECTION_CONFIG if _PROMPT_INJECTION_CONFIG.exists() else None,
            block_threshold=_THRESHOLD_MAP.get(prompt_injection_block_threshold, ThreatLevel.MEDIUM),
        ))
    if enable_credential_redactor:
        pre_middleware.append(CredentialRedactorGuardMiddleware(
            agent_id=agent_id,
            audit_log=audit,
            mode=credential_mode,
        ))
    if enable_context_budget:
        scheduler = ContextScheduler(total_budget=context_budget_total_tokens)
        pre_middleware.append(ContextBudgetGuardMiddleware(
            agent_id=agent_id,
            scheduler=scheduler,
            audit_log=audit,
        ))

    toolkit_middleware = create_governance_middleware(
        policy_directory=_POLICY_DIR,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        agent_id=agent_id,
        enable_rogue_detection=enable_rogue_detection,
        audit_log=audit,
    )

    middleware = pre_middleware + list(toolkit_middleware)

    logger.info(
        "governance.stack_built",
        extra={
            "agent_id": agent_id,
            "policy_dir": str(_POLICY_DIR),
            "middleware_count": len(middleware),
            "postgres_connected": pg_backend._pool is not None,
            "guards": {
                "prompt_injection":   enable_prompt_injection_guard,
                "credential_guard":   enable_credential_redactor,
                "context_budget":     enable_context_budget,
                "rogue_detection":    enable_rogue_detection,
            },
        },
    )
    return middleware, pg_backend, audit
