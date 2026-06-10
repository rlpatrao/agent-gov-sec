"""
governance.extensions.reasoning_guard — Gap 4: reasoning-chain guardrails.

Today guards fire at each agent's I/O boundary (prompt-injection, credential
redaction, context budget pre-call; audit post-call) and every A2A hop is
governed — but nothing inspects the *intermediate* reasoning: the plan and
tool-selection steps the model proposes before they execute. This module adds a
**pre-execution validator** for those steps.

Each ``ReasoningStep`` (a planned tool call or data access extracted from the
model's plan / ``reasoning_tokens``) is checked against policy *before* it runs:
  - tool-selection → the agent's capability allow-list (deny-unknown);
  - data access    → the Gap-1 ``DataAccessMediator`` (out-of-scope dataset → deny).

This is an I/O-style policy check on reasoning *structure*, not semantic CoT
analysis (that's the research-y 7.4.3, intentionally out of scope here). The
captured-reasoning structure is shared with ``reasoning_trace`` (Gap 4+) so a
chain is captured once and both validated and logged.

Feature-flagged off by default (``GALAXY_GAP_REASONING_GUARD``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReasoningStep:
    """One inspectable step from the model's plan / tool-selection."""
    kind: str                                   # "tool_call" | "data_access" | "plan"
    tool: Optional[str] = None
    dataset: Optional[str] = None
    table: Optional[str] = None
    columns: tuple[str, ...] = ()
    text: str = ""                              # the reasoning snippet (for the trace)


@dataclass
class StepVerdict:
    step: ReasoningStep
    allowed: bool
    reason: str = ""
    signals: list[str] = field(default_factory=list)


@dataclass
class PlanVerdict:
    agent_type: str
    verdicts: list[StepVerdict] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return all(v.allowed for v in self.verdicts)

    @property
    def first_denial(self) -> Optional[StepVerdict]:
        return next((v for v in self.verdicts if not v.allowed), None)


class ReasoningStepValidator:
    """Validates planned reasoning steps against capability + data-scope policy
    before any of them execute."""

    def __init__(self, mediator: Optional[Any] = None) -> None:
        # mediator: governance.extensions.data_fgac.DataAccessMediator (for data_access steps)
        self._mediator = mediator

    def validate_step(
        self,
        *,
        agent_type: str,
        step: ReasoningStep,
        allowed_tools: set[str],
    ) -> StepVerdict:
        if step.kind == "tool_call":
            if step.tool and step.tool in allowed_tools:
                return StepVerdict(step, True, "tool in allow-list")
            return StepVerdict(
                step, False,
                f"tool '{step.tool}' not in capability allow-list", ["capability_violation"],
            )

        if step.kind == "data_access":
            if self._mediator is None or not step.dataset or not step.table:
                # No mediator wired / underspecified → fail-closed on data access.
                return StepVerdict(step, False, "data access not authorizable (no mediator/target)", ["unscoped_data_access"])
            decision = self._mediator.authorize(
                agent_type=agent_type, dataset=step.dataset, table=step.table,
                columns=list(step.columns),
            )
            if decision.denied:
                return StepVerdict(step, False, decision.reason, ["data_out_of_scope"])
            signals = ["data_masked"] if decision.masked_columns else []
            return StepVerdict(step, True, decision.reason, signals)

        # "plan" / narrative steps: allowed (no executable action to gate).
        return StepVerdict(step, True, "non-actionable reasoning step")

    def validate_plan(
        self,
        *,
        agent_type: str,
        steps: list[ReasoningStep],
        allowed_tools: set[str],
    ) -> PlanVerdict:
        verdicts = [
            self.validate_step(agent_type=agent_type, step=s, allowed_tools=allowed_tools)
            for s in steps
        ]
        plan = PlanVerdict(agent_type=agent_type, verdicts=verdicts)
        denial = plan.first_denial
        if denial is not None:
            logger.warning(
                "reasoning_guard.step_denied",
                extra={"agent_type": agent_type, "reason": denial.reason, "signals": denial.signals},
            )
        return plan
