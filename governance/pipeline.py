"""
governance.pipeline — the framework-neutral guard pipeline.

This is the governance orchestration, lifted out of any agent framework. A
``GuardPipeline`` composes the cloud-neutral primitives (``agent_os``
PromptInjectionDetector / CredentialRedactor / ContextScheduler plus this repo's
WS7 extensions: FGAC mediator, reasoning-step validator, CoT/CoVe trace) and runs
the fixed governance sequence at three framework-agnostic hooks:

  before_model(text)        prompt-injection (B4) → credential (B5) → budget (B6)
  after_model(response)     reasoning-trace capture (CoT/CoVe)            (G20)
  before_tool(name, args)   capability/reasoning-step (B7/G19) → blocked-pattern (B8)

Every framework adapter (LangGraph middleware, the raw loop, Pydantic AI) is a
thin shim that maps its own hooks onto these three calls — no governance *logic*
lives in any adapter. Blocks raise ``GovernanceViolation``. Decisions are written
to the shared ``GovernanceAuditLogger`` (stdout + OTel + hash-chained ledger).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import governance
from agent_os.audit_logger import (
    AuditEntry,
    GovernanceAuditLogger,
    InMemoryBackend,
    LoggingBackend,
)
from agent_os.context_budget import BudgetExceeded, ContextPriority, ContextScheduler
from agent_os.credential_redactor import CredentialRedactor
from agent_os.prompt_injection import PromptInjectionDetector, ThreatLevel, load_prompt_injection_config

from governance.adapters.otel_audit_backend import OtelAuditBackend
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_drift import DataAccessDriftDetector, JsonFileBaselineStore
from governance.extensions.data_fgac import DataAccessMediator
from governance.extensions.reasoning_guard import ReasoningStep, ReasoningStepValidator
from governance.extensions.reasoning_trace import ReasoningTraceLogger

logger = logging.getLogger(__name__)

_GOVERNANCE_DIR = Path(governance.__file__).parent
_PROMPT_INJECTION_CONFIG = _GOVERNANCE_DIR / "configs" / "prompt-injection.yaml"

_THRESHOLD_MAP: dict[str, ThreatLevel] = {
    "low": ThreatLevel.LOW,
    "medium": ThreatLevel.MEDIUM,
    "high": ThreatLevel.HIGH,
    "critical": ThreatLevel.CRITICAL,
}
_THREAT_RANK: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class GovernanceViolation(Exception):
    """Raised by a guard hook to block a model or tool call. Carries a machine
    code so callers / the demo can assert which control fired."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _build_injection_detector() -> PromptInjectionDetector:
    """Construct the detector, backfilling the toolkit-config attrs that
    ``load_prompt_injection_config`` omits (without these it fails-closed)."""
    if _PROMPT_INJECTION_CONFIG.exists():
        cfg = load_prompt_injection_config(str(_PROMPT_INJECTION_CONFIG))
        for attr in ("allowlist", "blocklist", "custom_patterns"):
            if not hasattr(cfg, attr):
                setattr(cfg, attr, [])
        if not hasattr(cfg, "sensitivity"):
            cfg.sensitivity = "balanced"
    else:
        cfg = None
    return PromptInjectionDetector(cfg)


class GuardPipeline:
    """Framework-neutral governance orchestration. All knobs are passed in (no env
    reads). Adapters call before_model / after_model / before_tool around their
    own model/tool execution."""

    def __init__(
        self,
        *,
        agent_id: str,
        agent_type: str,
        nhi_id: str,
        run_id: str,
        audit_log: GovernanceAuditLogger,
        allowed_tools: Optional[list[str]] = None,
        blocked_patterns: Optional[list[str]] = None,
        prompt_injection_block_threshold: str = "medium",
        enable_prompt_injection_guard: bool = True,
        enable_credential_redactor: bool = True,
        credential_mode: str = "redact",
        enable_context_budget: bool = True,
        context_budget_tokens: int = 8000,
        mediator: Optional[DataAccessMediator] = None,
        reasoning_validator: Optional[ReasoningStepValidator] = None,
        reasoning_trace: Optional[ReasoningTraceLogger] = None,
    ) -> None:
        self._agent_id = agent_id
        self._agent_type = agent_type
        self._nhi_id = nhi_id
        self._run_id = run_id
        self._audit = audit_log
        self._allowed_tools = set(allowed_tools or [])
        self._blocked_patterns = list(blocked_patterns or [])

        self._enable_pi = enable_prompt_injection_guard
        self._detector = _build_injection_detector() if enable_prompt_injection_guard else None
        self._block_at = _THREAT_RANK[prompt_injection_block_threshold]

        self._enable_cred = enable_credential_redactor
        self.redactor = CredentialRedactor() if enable_credential_redactor else None
        self._cred_mode = credential_mode

        self._enable_budget = enable_context_budget
        self._scheduler = ContextScheduler(total_budget=context_budget_tokens) if enable_context_budget else None

        self._mediator = mediator
        self._reasoning = reasoning_validator
        self._trace = reasoning_trace

    # ── per-model-call governance (B4/B5/B6) ────────────────────────────────
    def before_model(self, text: str) -> bool:
        """Run the pre-call guards. Returns ``True`` if the caller should redact
        credentials from the outgoing messages in place (credential redact mode).
        Raises ``GovernanceViolation`` on a block."""
        if logger.isEnabledFor(logging.DEBUG):
            safe = (self.redactor.redact(text) if self.redactor else text) or ""
            logger.debug("guard.prompt   agent=%s nhi=%s :: %r", self._agent_type, self._nhi_id, safe[:240])

        # 1. Prompt injection (B4)
        if self._enable_pi and self._detector and text:
            result = self._detector.detect(text, source=self._agent_id)
            rank = _THREAT_RANK.get(result.threat_level.value, 0)
            blocked = result.is_injection and rank >= self._block_at
            self._log(
                "prompt_injection_check", "prompt_injection_detect",
                "deny" if blocked else ("audit" if result.is_injection else "allow"),
                result.explanation,
                {"threat": result.threat_level.value,
                 "type": result.injection_type.value if result.injection_type else "none",
                 "confidence": round(result.confidence, 3)},
            )
            if blocked:
                raise GovernanceViolation(
                    "prompt_injection",
                    f"Prompt injection blocked (threat={result.threat_level.value}, "
                    f"confidence={result.confidence:.2f})",
                )

        # 2. Credential redactor (B5)
        should_redact = False
        if self._enable_cred and self.redactor and text and self.redactor.contains_credentials(text):
            types = sorted({m.name for m in self.redactor.find_matches(text)})
            if self._cred_mode == "deny":
                self._log("credential_check", "credential_scan", "deny",
                          f"Credential leak blocked: {', '.join(types)}",
                          {"credential_types": types, "mode": "deny"})
                raise GovernanceViolation("credential_leak", f"Credential leak blocked: {', '.join(types)}")
            should_redact = True
            self._log("credential_check", "credential_scan", "audit",
                      f"Redacted credential(s): {', '.join(types)}",
                      {"credential_types": types, "mode": "redact"})

        # 3. Context budget (B6)
        if self._enable_budget and self._scheduler and text:
            estimated = max(1, len(text) // 4)
            try:
                window = self._scheduler.allocate(
                    agent_id=self._agent_id, task="model_call", priority=ContextPriority.NORMAL,
                )
            except BudgetExceeded as e:
                self._log("context_budget_check", "context_budget", "deny", str(e), {"estimated_tokens": estimated})
                raise GovernanceViolation("context_budget", f"Context budget exhausted: {e}") from e
            try:
                if estimated > window.total:
                    self._log("context_budget_check", "context_budget", "deny",
                              f"prompt {estimated} > window {window.total}", {"estimated_tokens": estimated})
                    raise GovernanceViolation(
                        "context_budget",
                        f"Estimated {estimated} tokens exceeds window {window.total}",
                    )
                self._log("context_budget_check", "context_budget", "allow",
                          f"allocated {window.total}; estimated {estimated}", {"estimated_tokens": estimated})
            finally:
                self._scheduler.release(self._agent_id)

        return should_redact

    # ── post-model-call (G20) ───────────────────────────────────────────────
    def after_model(self, response_text: str) -> None:
        if self._trace is not None:
            try:
                self._trace.capture(
                    run_id=self._run_id, agent_type=self._agent_type, nhi_id=self._nhi_id,
                    cot=response_text, decision="allow", module_id=self._agent_id,
                )
            except Exception as e:  # observability must never break the run
                logger.warning("governance.reasoning_trace_failed", extra={"error": str(e)})

    # ── per-tool-call governance (B7/G19, B8) ───────────────────────────────
    def before_tool(self, name: str, args: Any) -> None:
        """Capability allow-list + blocked-pattern scan. Raises on a block."""
        if self._reasoning is not None:
            step = ReasoningStep(kind="tool_call", tool=name)
            verdict = self._reasoning.validate_step(
                agent_type=self._agent_type, step=step, allowed_tools=self._allowed_tools,
            )
            if not verdict.allowed:
                self._log("capability_check", f"tool:{name}", "deny", verdict.reason,
                          {"tool": name, "signals": verdict.signals})
                raise GovernanceViolation(verdict.signals[0] if verdict.signals else "capability_violation", verdict.reason)

        arg_str = str(args)
        for pat in self._blocked_patterns:
            if pat.lower() in arg_str.lower():
                self._log("policy_check", f"tool:{name}", "deny",
                          f"blocked pattern '{pat}' in tool args", {"tool": name, "pattern": pat})
                raise GovernanceViolation("blocked_pattern", f"Blocked pattern '{pat}' in tool '{name}' arguments")

        self._log("capability_check", f"tool:{name}", "allow", "tool permitted", {"tool": name})

    # ── audit helper ────────────────────────────────────────────────────────
    def _log(self, event_type: str, action: str, decision: str, reason: str, meta: dict) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            tag = "INTERCEPTED" if decision in ("deny", "block") else decision.upper()
            logger.debug("guard.verdict  agent=%s %-11s %-22s :: %s",
                         self._agent_type, tag, event_type, reason)
        self._audit.log(AuditEntry(
            event_type=event_type, agent_id=self._agent_id, action=action,
            decision=decision, reason=reason,
            metadata={"run_id": self._run_id, "nhi_id": self._nhi_id,
                      "agent_type": self._agent_type, **meta},
        ))


async def build_guard_pipeline(
    *,
    agent_id: str,
    agent_type: str,
    nhi_id: str,
    run_id: str,
    allowed_tools: Optional[list[str]] = None,
    blocked_patterns: Optional[list[str]] = None,
    prompt_injection_block_threshold: str = "medium",
    enable_prompt_injection_guard: bool = True,
    enable_credential_redactor: bool = True,
    credential_mode: str = "redact",
    enable_context_budget: bool = True,
    context_budget_tokens: int = 8000,
    enable_data_fgac: bool = False,
    enable_data_drift: bool = False,
    enable_reasoning_guard: bool = False,
    enable_reasoning_trace: bool = False,
    catalog: Optional[DataClassificationCatalog] = None,
    mediator: Optional[DataAccessMediator] = None,
) -> tuple["GuardPipeline", Any, GovernanceAuditLogger, DataAccessMediator | None]:
    """Assemble the audit logger + hash-chain ledger + governance primitives and
    return ``(pipeline, ledger, audit_logger, mediator)`` — framework-agnostic.

    The mediator is returned so the agent's data tools read through the *same*
    FGAC decision point the pipeline validates against. The ledger backend is
    resolved via the selected cloud provider (azure → Postgres, aws → DynamoDB,
    gcp → BigQuery, local → in-memory) — ``CLOUD_PROVIDER`` picks it, nothing is
    hardcoded here."""
    audit = GovernanceAuditLogger()
    audit.add_backend(InMemoryBackend())        # introspection (demo/tests)
    audit.add_backend(LoggingBackend())          # stdout
    audit.add_backend(OtelAuditBackend())        # App Insights / CloudWatch
    from core.provider_factory import get_provider
    ledger = await get_provider().audit_backend(run_id=run_id)
    audit.add_backend(ledger)

    drift = DataAccessDriftDetector(store=JsonFileBaselineStore()) if enable_data_drift else None
    if enable_data_fgac and mediator is None:
        mediator = DataAccessMediator(
            catalog=catalog or DataClassificationCatalog.load(),
            drift_detector=drift,
        )
    elif not enable_data_fgac:
        mediator = None
    reasoning_validator = (
        ReasoningStepValidator(mediator=mediator) if enable_reasoning_guard else None
    )
    reasoning_trace = ReasoningTraceLogger(audit_backend=ledger) if enable_reasoning_trace else None

    pipeline = GuardPipeline(
        agent_id=agent_id, agent_type=agent_type, nhi_id=nhi_id, run_id=run_id, audit_log=audit,
        allowed_tools=allowed_tools, blocked_patterns=blocked_patterns,
        prompt_injection_block_threshold=prompt_injection_block_threshold,
        enable_prompt_injection_guard=enable_prompt_injection_guard,
        enable_credential_redactor=enable_credential_redactor, credential_mode=credential_mode,
        enable_context_budget=enable_context_budget, context_budget_tokens=context_budget_tokens,
        mediator=mediator, reasoning_validator=reasoning_validator, reasoning_trace=reasoning_trace,
    )

    logger.info("governance.pipeline.built",
                extra={"agent_id": agent_id, "fgac": enable_data_fgac, "drift": enable_data_drift,
                       "reasoning_guard": enable_reasoning_guard, "reasoning_trace": enable_reasoning_trace})
    return pipeline, ledger, audit, mediator
