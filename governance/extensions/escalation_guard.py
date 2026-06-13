"""
governance.extensions.escalation_guard — human-in-the-loop approval guard.

Wraps ``agent_os.escalation.EscalationManager`` so a tool call that the policy
flags as requiring human approval is held until an approver responds. The guard
exposes a synchronous gate (``requires_approval``) for the cheap "does this
action need escalation at all?" question, and an asynchronous
``approve_tool`` coroutine that awaits the manager's ``request_approval`` and
maps the outcome onto a :class:`GuardDecision`.

Fail-closed: when the configured policy uses ``default_on_timeout='deny'`` (the
default), a non-responding approver yields ``approved=False`` and the guard
blocks with code ``escalation_denied``. An ``AUTO_APPROVED`` outcome (the action
does not require approval) is treated as a pass, not an escalation event.

The module does not import the pipeline; it returns a verdict and lets the
pipeline map a block onto a ``GovernanceViolation``.
"""

from __future__ import annotations

from typing import Any, Optional

from agent_os.escalation import (
    EscalationManager,
    EscalationOutcome,
    EscalationPolicy,
)

from governance.extensions.decision import GuardDecision

BLOCK_CODE = "escalation_denied"


class HumanEscalationGuard:
    """Hold tool calls that require human approval until an approver responds.

    Construct with either an existing :class:`EscalationManager` or the
    arguments to build one. The constructor builds the manager once so the
    per-call methods stay pure.
    """

    def __init__(
        self,
        manager: Optional[EscalationManager] = None,
        *,
        policy: Optional[EscalationPolicy] = None,
        actions_requiring_approval: Optional[list[str]] = None,
        timeout_seconds: int = 300,
        default_on_timeout: str = "deny",
    ) -> None:
        if manager is not None:
            self._manager = manager
        else:
            if policy is None:
                policy = EscalationPolicy(
                    actions_requiring_approval=actions_requiring_approval or [],
                    timeout_seconds=timeout_seconds,
                    default_on_timeout=default_on_timeout,
                )
            self._manager = EscalationManager(policy)

    @property
    def manager(self) -> EscalationManager:
        return self._manager

    def requires_approval(self, name: str, **ctx: Any) -> bool:
        """Cheap synchronous gate: does this action need human approval?

        Matches on exact action name, regex pattern, or a context
        ``classification`` value, per the underlying policy. Forward the data
        classification through ``ctx`` so classification-based rules fire.
        """
        return self._manager.requires_approval(name, **ctx)

    async def approve_tool(
        self,
        agent_id: str,
        name: str,
        args: dict[str, Any],
        classification: Optional[str] = None,
    ) -> GuardDecision:
        """Await human approval for a tool call and return a verdict.

        ``classification`` (when supplied) is forwarded into the escalation
        context so classification-based approval rules can match. An
        ``AUTO_APPROVED`` or ``APPROVED`` outcome allows; a denial or timeout
        (``default_on_timeout='deny'``) blocks with ``escalation_denied``.
        """
        context: dict[str, Any] = {"args": args}
        if classification is not None:
            context["classification"] = classification

        decision = await self._manager.request_approval(
            agent_id, name, context=context
        )

        if decision.outcome == EscalationOutcome.AUTO_APPROVED:
            return GuardDecision(
                allowed=True,
                reason="Action does not require human approval",
                signals=["escalation_not_required"],
                metadata={"outcome": decision.outcome.value},
            )

        if decision.approved:
            return GuardDecision(
                allowed=True,
                reason=decision.reason or "Human approver granted the request",
                signals=["escalation_approved"],
                metadata={
                    "request_id": decision.request_id,
                    "outcome": decision.outcome.value,
                    "decided_by": decision.decided_by,
                },
            )

        return GuardDecision.block(
            BLOCK_CODE,
            decision.reason
            or f"Human approval not granted (outcome={decision.outcome.value})",
            signals=["escalation_denied", decision.outcome.value],
            request_id=decision.request_id,
            outcome=decision.outcome.value,
            decided_by=decision.decided_by,
        )
