"""
governance.shared.enforcement.session — the single enforcement code path.

`build_enforcement(policy)` constructs a `GuardPipeline` (the real `agent_os` /
`agent_sre` bindings) from a resolved control policy and wraps it in an
`EnforcementSession` with a synchronous, transport-neutral surface
(`check_input` / `check_tool` / `check_output`). The in-process middleware and
the out-of-process chokepoints both drive the *same* session, so trust-but-verify
re-runs identical control logic rather than a weaker re-implementation — this is
what replaced the former minimal `enforcement_core`.

Unlike the async `build_guard_pipeline` (which also wires the cloud audit ledger
and FGAC mediator for a live in-process run), `build_enforcement` is synchronous
and self-contained: an in-memory audit log, no cloud-provider dependency. That
keeps it usable inside a Lambda/interceptor or a daemon without pulling the
provider stack. Data-layer FGAC is enforced at the data chokepoint (it owns the
mediator), not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agent_os.audit_logger import GovernanceAuditLogger, InMemoryBackend, LoggingBackend

from governance.shared.enforcement.output_pii import OutputPiiGuard
from governance.shared.enforcement.pipeline import (
    GovernanceViolation,
    GuardPipeline,
    _register_flag_gated_guards,
)
from governance.shared.enforcement.reasoning_guard import ReasoningStepValidator


@dataclass
class Verdict:
    """Outcome of one enforcement step. `text` carries any redaction applied."""

    blocked: bool = False
    code: str = ""
    reason: str = ""
    text: str = ""


class EnforcementSession:
    """Synchronous wrapper over a `GuardPipeline`. Translates the pipeline's
    raise-on-block / transform-on-redact behaviour into uniform `Verdict`s."""

    def __init__(self, pipeline: GuardPipeline) -> None:
        self.pipeline = pipeline

    def check_input(self, text: str) -> Verdict:
        """Input guards (injection, credential, budget). Returns a Verdict whose
        `text` is credential-redacted when the policy is in redact mode."""
        try:
            should_redact = self.pipeline.before_model(text or "")
        except GovernanceViolation as v:
            return Verdict(blocked=True, code=v.code, reason=str(v), text=text or "")
        out = text or ""
        if should_redact and self.pipeline.redactor is not None:
            out = self.pipeline.redactor.redact(out)
        return Verdict(text=out)

    def check_tool(self, name: str, args: Any) -> Verdict:
        """Capability allow-list + blocked-pattern scan over a tool call."""
        try:
            self.pipeline.before_tool(name, args)
        except GovernanceViolation as v:
            return Verdict(blocked=True, code=v.code, reason=str(v))
        return Verdict()

    def check_output(self, text: str) -> Verdict:
        """Output guards (PII/credential redaction, content-safety). Returns the
        possibly-redacted text; blocked when an output guard rejects."""
        try:
            out = self.pipeline.after_model(text or "")
        except GovernanceViolation as v:
            return Verdict(blocked=True, code=v.code, reason=str(v), text=text or "")
        return Verdict(text=out)


def build_enforcement(
    policy: dict,
    *,
    agent_id: str = "remote",
    agent_type: Optional[str] = None,
    nhi_id: Optional[str] = None,
    run_id: str = "remote",
    mediator: Any = None,
    register_sweep: bool = True,
) -> EnforcementSession:
    """Build an `EnforcementSession` from a resolved policy dict (the shape
    produced by `policy_registry.ControlPolicy.to_dict()`). Used by both the
    in-process tier and the remote chokepoints."""
    mb = policy.get("model_boundary") or {}
    audit = GovernanceAuditLogger()
    audit.add_backend(InMemoryBackend())
    audit.add_backend(LoggingBackend())

    pipeline = GuardPipeline(
        agent_id=agent_id,
        agent_type=agent_type or policy.get("agent_type") or agent_id,
        nhi_id=nhi_id or agent_id,
        run_id=run_id,
        audit_log=audit,
        allowed_tools=list(policy.get("allowed_tools") or []),
        blocked_patterns=list(mb.get("blocked_patterns") or []),
        prompt_injection_block_threshold=mb.get("injection_threshold", "medium"),
        enable_prompt_injection_guard=mb.get("injection_enabled", True),
        enable_credential_redactor=mb.get("credential_enabled", True),
        credential_mode=mb.get("credential_mode", "redact"),
        enable_context_budget=mb.get("budget_enabled", True),
        context_budget_tokens=mb.get("budget_max_tokens", 8000),
        mediator=mediator,
        # Wire the capability/reasoning validator so the tool allow-list is
        # enforced at before_tool (the pipeline only checks it when present).
        reasoning_validator=ReasoningStepValidator(mediator=mediator),
    )

    if register_sweep:
        _register_flag_gated_guards(pipeline, agent_id)

    # Output PII/credential redaction is part of the model boundary, so enable it
    # at the boundary regardless of the GALAXY_* flag (unless already wired).
    if mb.get("output_pii_enabled", True) and not any(
        label == "output_pii" for label, _ in pipeline._after_model_guards
    ):
        op = OutputPiiGuard()
        pipeline.register_after_model("output_pii", lambda text, _g=op: _g.redact_output(text))

    return EnforcementSession(pipeline)
