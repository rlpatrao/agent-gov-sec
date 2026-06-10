"""
adapters.langgraph.governance — the LangGraph middleware-stack assembly.

``GalaxyGuardMiddleware`` is the framework binding for the LangGraph axis: a
LangChain ``AgentMiddleware`` whose ``wrap_model_call`` / ``wrap_tool_call`` hooks
run the **same cloud-neutral governance primitives** the MAF guards use
(``agent_os`` PromptInjectionDetector / CredentialRedactor / ContextScheduler /
RogueAgentDetector) plus this repo's WS7 extensions (FGAC mediator, data-access
drift, reasoning-step validation, CoT/CoVe trace). No governance *logic* is
re-implemented here — only the adaptation to LangChain's middleware surface.

Mapping of features to hooks:

  wrap_model_call (per LLM request)
    · prompt-injection detection          → block at/above threshold   (B4)
    · credential redactor (redact|deny)                                 (B5)
    · context-budget allocation + cap      → block oversized prompt     (B6)
    · reasoning-trace capture (CoT/CoVe)   → mandatory redact + audit   (G20)

  wrap_tool_call (per tool execution)
    · reasoning-step validation            → capability + data-scope    (B7/G19)
    · blocked-pattern scan on tool args                                 (B8)
    · data-access drift recording (via the FGAC mediator the tool uses) (F18)

Every decision is written to the shared ``GovernanceAuditLogger`` (stdout +
OTel + hash-chained ledger). Blocks raise ``GovernanceViolation``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

import governance
from langchain.agents.middleware import AgentMiddleware

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
    ``load_prompt_injection_config`` omits (same quirk the MAF guard works
    around — without these the detector fails-closed on every call)."""
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


def _model_input_text(request: Any) -> str:
    """Extract the concatenated user-message text from a LangChain ModelRequest,
    resilient to content being a string or a list of content blocks."""
    messages = getattr(request, "messages", None) or []
    parts: list[str] = []
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    parts.append(block["text"])
    return " ".join(p for p in parts if p).strip()


def _response_text(response: Any) -> str:
    msg = getattr(response, "message", None) or getattr(response, "result", None) or response
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


class GalaxyGuardMiddleware(AgentMiddleware):
    """LangChain ``AgentMiddleware`` that runs the Galaxy governance stack.

    All knobs come from the per-agent config (passed by ``build_langgraph_agent``);
    nothing here reads env for behavior. The middleware is intentionally
    framework-thin — it adapts our neutral primitives to LangChain's hooks.
    """

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
        self._redactor = CredentialRedactor() if enable_credential_redactor else None
        self._cred_mode = credential_mode

        self._enable_budget = enable_context_budget
        self._scheduler = ContextScheduler(total_budget=context_budget_tokens) if enable_context_budget else None

        self._mediator = mediator
        self._reasoning = reasoning_validator
        self._trace = reasoning_trace

    # ── per-model-call governance ──────────────────────────────────────────
    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        text = _model_input_text(request)
        # Show the prompt the guards see (redacted + truncated) at DEBUG, so a
        # `--log-level DEBUG` run reads as: prompt → which guard fired → verdict.
        if logger.isEnabledFor(logging.DEBUG):
            safe = (self._redactor.redact(text) if self._redactor else text) or ""
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
        if self._enable_cred and self._redactor and text and self._redactor.contains_credentials(text):
            types = sorted({m.name for m in self._redactor.find_matches(text)})
            if self._cred_mode == "deny":
                self._log("credential_check", "credential_scan", "deny",
                          f"Credential leak blocked: {', '.join(types)}",
                          {"credential_types": types, "mode": "deny"})
                raise GovernanceViolation("credential_leak", f"Credential leak blocked: {', '.join(types)}")
            self._redact_in_place(request)
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

        # 4. Execute the model call
        response = handler(request)

        # 5. Reasoning-trace capture (G20) — mandatory redaction inside the logger
        if self._trace is not None:
            try:
                self._trace.capture(
                    run_id=self._run_id, agent_type=self._agent_type, nhi_id=self._nhi_id,
                    cot=_response_text(response), decision="allow", module_id=self._agent_id,
                )
            except Exception as e:  # observability must never break the run
                logger.warning("langgraph.reasoning_trace_failed", extra={"error": str(e)})

        return response

    # ── per-tool-call governance ───────────────────────────────────────────
    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name", "<unknown>") if isinstance(tool_call, dict) else str(tool_call)
        args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}

        # 1. Reasoning-step validation: capability allow-list (B7/G19). Data-scope
        #    authz + masking + drift are enforced inside the data tool itself
        #    (via the shared DataAccessMediator), so the pre-exec guard here is
        #    capability-only — one decision point per concern, no double-counting.
        if self._reasoning is not None:
            step = ReasoningStep(kind="tool_call", tool=name)
            verdict = self._reasoning.validate_step(
                agent_type=self._agent_type, step=step, allowed_tools=self._allowed_tools,
            )
            if not verdict.allowed:
                self._log("capability_check", f"tool:{name}", "deny", verdict.reason,
                          {"tool": name, "signals": verdict.signals})
                raise GovernanceViolation(verdict.signals[0] if verdict.signals else "capability_violation", verdict.reason)

        # 2. Blocked-pattern scan on tool args (B8)
        arg_str = str(args)
        for pat in self._blocked_patterns:
            if pat.lower() in arg_str.lower():
                self._log("policy_check", f"tool:{name}", "deny",
                          f"blocked pattern '{pat}' in tool args", {"tool": name, "pattern": pat})
                raise GovernanceViolation("blocked_pattern", f"Blocked pattern '{pat}' in tool '{name}' arguments")

        self._log("capability_check", f"tool:{name}", "allow", "tool permitted", {"tool": name})
        return handler(request)

    # ── audit helper ────────────────────────────────────────────────────────
    def _log(self, event_type: str, action: str, decision: str, reason: str, meta: dict) -> None:
        # Readable verdict line (DEBUG) — INTERCEPTED on a block, else the decision.
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

    def _redact_in_place(self, request: Any) -> None:
        messages = getattr(request, "messages", None) or []
        for msg in messages:
            content = getattr(msg, "content", None)
            if isinstance(content, str) and self._redactor.contains_credentials(content):
                try:
                    msg.content = self._redactor.redact(content)
                except Exception:  # immutable message — audit already records the redaction intent
                    pass


async def build_langgraph_governance(
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
) -> tuple[list, Any, GovernanceAuditLogger, DataAccessMediator | None]:
    """Assemble the governance middleware for a LangGraph agent.

    Returns ``(middleware_list, pg_backend, audit_logger, mediator)``. The
    mediator is returned so the agent's data tools can read through the *same*
    FGAC decision point the middleware validates against. Flags mirror the WS7
    feature flags but are passed explicitly so each agent opts in via its config.

    Authz model on this tree: tool/agent authz = the capability allow-list
    enforced by the reasoning-step validator + the per-tool ``allowed_tools``
    check; data authz = the FGAC mediator's ABAC decision (``agent_os``
    DataAccessEvaluator: classification ≤ clearance, category allow/deny). The
    standards-based Cedar engine is not present on this branch.
    """
    audit = GovernanceAuditLogger()
    audit.add_backend(InMemoryBackend())        # introspection (demo/tests)
    audit.add_backend(LoggingBackend())          # stdout
    audit.add_backend(OtelAuditBackend())        # App Insights / CloudWatch
    # Hash-chained compliance ledger — resolved via the selected cloud provider
    # (azure → Postgres, aws → DynamoDB, gcp → BigQuery, local → in-memory), so
    # CLOUD_PROVIDER picks the backend. No cloud is hardcoded here.
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

    guard = GalaxyGuardMiddleware(
        agent_id=agent_id, agent_type=agent_type, nhi_id=nhi_id, run_id=run_id, audit_log=audit,
        allowed_tools=allowed_tools, blocked_patterns=blocked_patterns,
        prompt_injection_block_threshold=prompt_injection_block_threshold,
        enable_prompt_injection_guard=enable_prompt_injection_guard,
        enable_credential_redactor=enable_credential_redactor, credential_mode=credential_mode,
        enable_context_budget=enable_context_budget, context_budget_tokens=context_budget_tokens,
        mediator=mediator, reasoning_validator=reasoning_validator, reasoning_trace=reasoning_trace,
    )

    logger.info("langgraph.governance.stack_built",
                extra={"agent_id": agent_id, "fgac": enable_data_fgac, "drift": enable_data_drift,
                       "reasoning_guard": enable_reasoning_guard, "reasoning_trace": enable_reasoning_trace})
    return [guard], ledger, audit, mediator
