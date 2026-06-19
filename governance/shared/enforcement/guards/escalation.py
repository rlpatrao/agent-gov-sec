"""
Escalation hook — surfaces governance denials to a human-in-the-loop queue
via `agent_os.escalation.EscalationManager`.

Today there is no production approver bound, so escalations land as audit
log entries and (when a callable is wired) any sink the operator hooks
in. The two natural targets when you wire it for real:

  - Slack/Teams webhook (`approval_handler` posts a card; humans click)
  - Azure Queue Storage / Service Bus (durable queue read by an oncall app)

The hook is intentionally pluggable: pass `approval_handler=None` and it's
audit-only. Pass a coroutine and you opt in to actual blocking-on-human.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger
from agent_os.escalation import (
    EscalationDecision,
    EscalationManager,
    EscalationOutcome,
    EscalationPolicy,
    EscalationRequest,
)

logger = logging.getLogger(__name__)


def build_escalation_manager(
    policy_actions: Optional[list[str]] = None,
    timeout_seconds: int = 60,
    approval_handler: Optional[Callable[[EscalationRequest], Awaitable[None]]] = None,
) -> EscalationManager:
    """Construct an EscalationManager.

    Opt-in by default: with no `policy_actions`, every request returns
    `EscalationOutcome.AUTO_APPROVED` immediately and `maybe_escalate`
    becomes a low-overhead audit hook. Wire `policy_actions` *and*
    `approval_handler` together to make it interactive.

    `default_on_timeout="deny"` is the safe posture once you flip it on:
    a silent operator means the call doesn't go through.
    """
    policy = EscalationPolicy(
        actions_requiring_approval=list(policy_actions or []),
        timeout_seconds=timeout_seconds,
        default_on_timeout="deny",
        max_auto_approvals_per_hour=0,
    )
    return EscalationManager(policy=policy, approval_handler=approval_handler)


async def maybe_escalate(
    manager: EscalationManager,
    *,
    agent_id: str,
    action: str,
    reason: str,
    audit_log: Optional[GovernanceAuditLogger],
    extra_context: Optional[dict] = None,
) -> EscalationDecision:
    """Audit-log the escalation request + delegate to the manager. Returns
    the decision so the caller can decide whether to proceed.

    Today this is fire-and-audit (no real human queue is bound), so the
    decision typically hits its default-on-timeout policy and returns DENY.
    Wire `approval_handler` on the manager to make it interactive.
    """
    decision = await manager.request_approval(
        agent_id=agent_id,
        action=action,
        context=extra_context or {},
        reason=reason,
    )
    if audit_log is not None:
        audit_log.log(AuditEntry(
            event_type="escalation",
            agent_id=agent_id,
            action=f"escalation:{action}",
            decision=decision.outcome.value if hasattr(decision.outcome, "value") else str(decision.outcome),
            reason=reason,
            metadata={"request_id": decision.request_id, "decided_by": decision.decided_by or ""},
        ))
    return decision
