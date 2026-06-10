"""
ContextBudgetGuardMiddleware — wraps `agent_os.context_budget.ContextScheduler`
as a MAF AgentMiddleware.

Replaces the YAML char-count regex rule for cost-ceiling enforcement.
Pre-dispatch: estimates token count, calls scheduler.allocate(); rejects on
BudgetExceeded. Post-dispatch: records actual usage from response (when
available) so the scheduler tracks real consumption across runs.

Token estimate: ~1 token per 4 characters (the same heuristic the original
hand-rolled cost_ceiling used). Prompt-injection guard runs before this,
so we estimate against the (potentially redacted) message.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from agent_framework._middleware import AgentMiddleware, MiddlewareTermination
from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger
from agent_os.context_budget import (
    BudgetExceeded,
    ContextPriority,
    ContextScheduler,
    ContextWindow,
)

logger = logging.getLogger(__name__)


class ContextBudgetGuardMiddleware(AgentMiddleware):
    """Pre-dispatch token-budget guard backed by a shared ContextScheduler."""

    def __init__(
        self,
        agent_id: str,
        scheduler: ContextScheduler,
        audit_log: Optional[GovernanceAuditLogger] = None,
        priority: ContextPriority = ContextPriority.NORMAL,
        per_call_max: Optional[int] = None,
    ) -> None:
        self._agent_id = agent_id
        self._scheduler = scheduler
        self._audit = audit_log
        self._priority = priority
        self._per_call_max = per_call_max

    async def process(self, context: Any, call_next: Callable[[], Awaitable[None]]) -> None:
        msgs = getattr(context, "messages", None) or []
        prompt_text = " ".join(getattr(m, "text", None) or str(m) for m in msgs)
        estimated = max(1, len(prompt_text) // 4)
        task = _intent_label(context)

        try:
            window: ContextWindow = self._scheduler.allocate(
                agent_id=self._agent_id,
                task=task,
                priority=self._priority,
                max_tokens=self._per_call_max,
            )
        except BudgetExceeded as e:
            self._audit_deny(estimated, reason=str(e))
            logger.info("context_budget.deny_allocate",
                        extra={"agent_id": self._agent_id, "estimated": estimated})
            raise MiddlewareTermination(f"Context budget exhausted: {e}") from e

        # Soft per-window check — if our estimate is bigger than the allocated
        # window, deny before dispatch to avoid the round-trip cost.
        if estimated > window.total:
            self._audit_deny(estimated, reason=f"prompt > window ({estimated} > {window.total})")
            self._scheduler.release(self._agent_id)
            raise MiddlewareTermination(
                f"Estimated {estimated} tokens exceeds window {window.total}. "
                "Trim the prompt or raise the per-agent budget."
            )

        try:
            await call_next()
        finally:
            # Record actual usage from the chat response if MAF surfaces it on
            # the context; otherwise record the estimate so SLOs still tick.
            actual_input, actual_output = _extract_usage(context, fallback_input=estimated)
            self._scheduler.record_usage(
                agent_id=self._agent_id,
                lookup_tokens=actual_input,
                reasoning_tokens=actual_output,
            )
            self._scheduler.release(self._agent_id)
            self._audit_allow(estimated, actual_input + actual_output, window)

    def _audit_allow(self, estimated: int, actual: int, window: ContextWindow) -> None:
        if self._audit is None:
            return
        self._audit.log(AuditEntry(
            event_type="context_budget_check",
            agent_id=self._agent_id,
            action="context_budget",
            decision="allow",
            reason=f"allocated {window.total}; estimated {estimated}; used {actual}",
            metadata={
                "estimated_tokens": estimated,
                "actual_tokens":    actual,
                "window_total":     window.total,
                "lookup_budget":    window.lookup_budget,
                "reasoning_budget": window.reasoning_budget,
            },
        ))

    def _audit_deny(self, estimated: int, reason: str) -> None:
        if self._audit is None:
            return
        self._audit.log(AuditEntry(
            event_type="context_budget_check",
            agent_id=self._agent_id,
            action="context_budget",
            decision="deny",
            reason=reason,
            metadata={"estimated_tokens": estimated},
        ))


def _intent_label(context: Any) -> str:
    """Best-effort task tag for ContextScheduler.allocate; falls back to 'agent_run'."""
    md = getattr(context, "metadata", {}) or {}
    return str(md.get("intent") or "agent_run")


def _extract_usage(context: Any, fallback_input: int) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) off context.result.usage if available."""
    result = getattr(context, "result", None)
    if result is None:
        return fallback_input, 0
    usage = getattr(result, "usage", None) or getattr(result, "usage_details", None)
    if usage is None:
        return fallback_input, 0
    in_t = getattr(usage, "input_token_count", None) or getattr(usage, "prompt_tokens", None) or fallback_input
    out_t = getattr(usage, "output_token_count", None) or getattr(usage, "completion_tokens", None) or 0
    return int(in_t), int(out_t)
