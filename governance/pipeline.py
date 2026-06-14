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
from typing import Any, Callable, Optional

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
from governance.extensions.decision import GuardDecision
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

        # Sweep-era guard registries. Each entry is (label, callable->GuardDecision).
        # ``build_guard_pipeline`` populates these from the enabled GALAXY_* flags;
        # an unconfigured pipeline keeps them empty, so behaviour is unchanged.
        # before_tool: fn(name, args) -> GuardDecision
        self._before_tool_guards: list[tuple[str, Callable[[str, Any], GuardDecision]]] = []
        # after_model: fn(text) -> GuardDecision (may set .output to transform text)
        self._after_model_guards: list[tuple[str, Callable[[str], GuardDecision]]] = []
        # after_tool: fn(name, result_text) -> GuardDecision (may set .output)
        self._after_tool_guards: list[tuple[str, Callable[[str, str], GuardDecision]]] = []
        # Circuit breaker is special: it records success/failure outcomes, so it is
        # held by reference (not just a before_tool callable) — see after_tool / on_tool_error.
        self._circuit_breaker: Any = None

    # ── sweep guard registration (called by build_guard_pipeline) ────────────
    def register_before_tool(self, label: str, fn: Callable[[str, Any], GuardDecision]) -> None:
        self._before_tool_guards.append((label, fn))

    def register_after_model(self, label: str, fn: Callable[[str], GuardDecision]) -> None:
        self._after_model_guards.append((label, fn))

    def register_after_tool(self, label: str, fn: Callable[[str, str], GuardDecision]) -> None:
        self._after_tool_guards.append((label, fn))

    def set_circuit_breaker(self, cb: Any) -> None:
        """Register the circuit-breaker guard. allow_call gates before_tool; the
        breaker's record_success/record_failure fire from after_tool / on_tool_error."""
        self._circuit_breaker = cb
        self.register_before_tool("circuit_breaker", lambda name, args: cb.allow_call(name))

    def _apply_guard_decision(self, event_type: str, action: str, decision: GuardDecision) -> None:
        """Audit + raise for a sweep guard verdict (block path)."""
        self._log(event_type, action, "deny", decision.reason,
                  {"code": decision.code, "signals": decision.signals, **decision.metadata})
        raise GovernanceViolation(decision.code or "policy_denied", decision.reason)

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

    # ── post-model-call (G20 + output guards) ───────────────────────────────
    def after_model(self, response_text: str) -> str:
        """Capture the CoT/CoVe trace (G20) and run output guards (content
        quality, output PII). Returns the response text, possibly redacted by an
        output guard; raises ``GovernanceViolation`` if an output guard blocks.
        Adapters that forward the model's text downstream should use the return
        value so output redaction takes effect."""
        if self._trace is not None:
            try:
                self._trace.capture(
                    run_id=self._run_id, agent_type=self._agent_type, nhi_id=self._nhi_id,
                    cot=response_text, decision="allow", module_id=self._agent_id,
                )
            except Exception as e:  # observability must never break the run
                logger.warning("governance.reasoning_trace_failed", extra={"error": str(e)})

        text = response_text
        for label, guard in self._after_model_guards:
            decision = guard(text)
            if not decision.allowed:
                self._apply_guard_decision("output_check", f"after_model:{label}", decision)
            if decision.output is not None and decision.output != text:
                self._log("output_check", f"after_model:{label}", "audit",
                          f"{label} transformed output", {"code": decision.code, **decision.metadata})
                text = decision.output
        return text

    # ── post-tool-call (after_tool: inbound result governance) ───────────────
    def after_tool(self, name: str, result: str) -> str:
        """Govern a tool's *output* before it re-enters the model context
        (e.g. MCP response scan). Records circuit-breaker success. Returns the
        result text, possibly sanitized; raises on a block."""
        if self._circuit_breaker is not None:
            try:
                self._circuit_breaker.record_success(name)
            except Exception as e:
                logger.debug("circuit_breaker.record_success_failed", extra={"error": str(e)})
        text = result if result is not None else ""
        for label, guard in self._after_tool_guards:
            decision = guard(name, text)
            if not decision.allowed:
                self._apply_guard_decision("tool_output_check", f"after_tool:{name}:{label}", decision)
            if decision.output is not None and decision.output != text:
                self._log("tool_output_check", f"after_tool:{name}:{label}", "audit",
                          f"{label} sanitized tool output", {"tool": name, "code": decision.code, **decision.metadata})
                text = decision.output
        return text

    def on_tool_error(self, name: str) -> None:
        """Adapters call this when a tool raises, so the circuit breaker records a
        failure (and opens after the configured threshold)."""
        if self._circuit_breaker is not None:
            try:
                self._circuit_breaker.record_failure(name)
            except Exception as e:
                logger.debug("circuit_breaker.record_failure_failed", extra={"error": str(e)})

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

        # Sweep-era before_tool guards (egress, circuit breaker, transparency,
        # semantic policy, code/diff/exec review, reversibility, constraint graph,
        # memory-write, MCP gateway/rate-limit, cost, escalation gate). Each is
        # registered only when its GALAXY_* flag is on; absent flags → no-op.
        for label, guard in self._before_tool_guards:
            decision = guard(name, args)
            if not decision.allowed:
                self._apply_guard_decision("policy_check", f"tool:{name}:{label}", decision)

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

    sweep = _register_sweep_guards(pipeline, agent_id)

    logger.info("governance.pipeline.built",
                extra={"agent_id": agent_id, "fgac": enable_data_fgac, "drift": enable_data_drift,
                       "reasoning_guard": enable_reasoning_guard, "reasoning_trace": enable_reasoning_trace,
                       "sweep_guards": sweep})
    return pipeline, ledger, audit, mediator


def _register_sweep_guards(pipeline: "GuardPipeline", agent_id: str) -> list[str]:
    """Register the shape-safe sweep guards whose GALAXY_* flag is enabled.

    Only guards that no-op on non-matching tool shapes (or block clearly-malicious
    input while passing benign input) are wired here, so enabling a flag never
    breaks an unrelated tool call. The context-specific guards — reversibility
    (fail-closed on unknown actions), content quality (heuristic scorer),
    transparency (fail-closed until confirmed), constraint graph and MCP
    gateway/rate-limit/session/signer/screen (deny-by-default / identity / transport),
    and human escalation (async approval) — are exercised in their dedicated demo
    sections, not blanket-applied to every agent. Returns the list of guards wired."""
    from governance.extensions import flags

    wired: list[str] = []

    def on(flag: str) -> bool:
        return flags.is_enabled(flag)

    def _args(a: Any) -> dict:
        return a if isinstance(a, dict) else {}

    # ── before_tool (shape-safe) ──────────────────────────────────────────────
    # Each closure binds its guard via a default arg (``_g=...``) so it captures
    # that specific instance, not whatever the loop variable ends up pointing at.
    if on(flags.EGRESS_POLICY):
        from governance.extensions.egress_guard import EgressGuard
        eg = EgressGuard()
        pipeline.register_before_tool("egress", lambda name, args, _g=eg: _g.check_tool(name, _args(args)))
        wired.append("egress")
    if on(flags.CIRCUIT_BREAKER):
        from governance.extensions.circuit_breaker_guard import CircuitBreakerGuard
        pipeline.set_circuit_breaker(CircuitBreakerGuard())
        wired.append("circuit_breaker")
    if on(flags.SEMANTIC_POLICY):
        from governance.extensions.semantic_policy_guard import SemanticPolicyGuard
        sp = SemanticPolicyGuard()
        pipeline.register_before_tool("semantic_policy", lambda name, args, _g=sp: _g.check_tool(name, args))
        wired.append("semantic_policy")
    if on(flags.SECURE_CODEGEN):
        from governance.extensions.secure_codegen_guard import SecureCodegenGuard
        sc = SecureCodegenGuard()
        pipeline.register_before_tool("secure_codegen", lambda name, args, _g=sc: _g.check_code(name, _args(args)))
        wired.append("secure_codegen")
    if on(flags.SECURE_EXEC):
        from governance.extensions.secure_exec import SecureExecGuard
        se = SecureExecGuard()
        pipeline.register_before_tool("secure_exec", lambda name, args, _g=se: _g.check_exec(name, _args(args)))
        wired.append("secure_exec")
    if on(flags.DIFF_POLICY):
        from governance.extensions.diff_policy_guard import DiffPolicyGuard
        dp = DiffPolicyGuard(blocked_paths=["*.env", "secrets/**"])
        pipeline.register_before_tool("diff_policy", lambda name, args, _g=dp: _g.check_diff(name, _args(args)))
        wired.append("diff_policy")
    if on(flags.MEMORY_GUARD):
        from governance.extensions.memory_guard import MemoryWriteGuard
        mg = MemoryWriteGuard()
        pipeline.register_before_tool("memory_guard", lambda name, args, _g=mg: _g.check_write(name, _args(args), source=agent_id))
        wired.append("memory_guard")
    if on(flags.COST_GUARD):
        from governance.extensions.cost_guard import CostGuard
        cg = CostGuard()
        def _cost(name: str, args: Any, _g: Any = cg) -> GuardDecision:
            a = _args(args)
            est = float(a.get("estimated_cost", a.get("cost_usd", 0.0)) or 0.0)
            return _g.check(agent_id, est)
        pipeline.register_before_tool("cost", _cost)
        wired.append("cost")

    # ── after_model (output-side) ─────────────────────────────────────────────
    if on(flags.OUTPUT_PII):
        from governance.extensions.output_pii import OutputPiiGuard
        op = OutputPiiGuard()
        pipeline.register_after_model("output_pii", lambda text, _g=op: _g.redact_output(text))
        wired.append("output_pii")

    # ── after_tool (inbound tool output) ──────────────────────────────────────
    if on(flags.MCP_RESPONSE_SCAN):
        from governance.extensions.mcp_response_guard import McpResponseGuard
        mr = McpResponseGuard()
        pipeline.register_after_tool("mcp_response", lambda name, text, _g=mr: _g.scan_result(name, text))
        wired.append("mcp_response")

    return wired
