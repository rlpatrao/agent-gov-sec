"""
governance.extensions.cost_guard — per-call budget guard.

Wraps ``agent_sre.cost.CostGuard``. Before a tool or model call the pipeline
calls ``check(agent_id, estimated_cost)``; the wrapper consults
``CostGuard.check_task`` and returns ``GuardDecision(allowed=False,
code='cost_limit_exceeded')`` when the underlying guard vetoes (estimated cost
over the per-task limit, or the agent's budget already throttled/killed). After
the call the pipeline calls ``charge(agent_id, task_id, actual_cost)``, which
debits the budget via ``CostGuard.check_and_charge`` and reports the same veto
once the daily limit drives escalation to KILL.

Quirks honored (per discovery notes):
  - ``check_task`` is fail-CLOSED on a per-task breach but a no-op pass when
    estimated_cost is 0.0 (the default). The wrapper therefore requires the
    caller to supply a real estimate; a real estimate is what exercises the
    veto path.
  - The daily limit escalates ALERT -> THROTTLE -> KILL via ``auto_throttle``
    and ``kill_switch_threshold``. Once the budget is killed, both
    ``check_task`` and ``check_and_charge`` return ``(False, ...)``; the
    wrapper surfaces both as ``cost_limit_exceeded`` blocks.

Flag-agnostic; the pipeline gates it behind ``GALAXY_OPS_COST_GUARD`` and maps
a block onto GovernanceViolation.
"""

from __future__ import annotations

from agent_sre.cost import CostGuard as _CostGuard

from governance.extensions.decision import GuardDecision


class CostGuard:
    """Per-call budget gate backed by ``agent_sre.cost.CostGuard``."""

    def __init__(
        self,
        per_task_limit: float = 2.0,
        per_agent_daily_limit: float = 100.0,
    ) -> None:
        self._guard = _CostGuard(
            per_task_limit=per_task_limit,
            per_agent_daily_limit=per_agent_daily_limit,
        )

    def check(self, agent_id: str, estimated_cost: float) -> GuardDecision:
        """Veto a pending call whose estimated cost breaches the budget.

        ``estimated_cost`` must be a real estimate; passing 0.0 makes the
        underlying ``check_task`` a no-op pass.
        """
        ok, reason = self._guard.check_task(agent_id, estimated_cost)
        if not ok:
            return GuardDecision.block(
                "cost_limit_exceeded",
                reason,
                signals=["cost_limit_exceeded"],
                agent_id=agent_id,
                estimated_cost=estimated_cost,
            )
        return GuardDecision.allow(
            reason=reason,
            agent_id=agent_id,
            estimated_cost=estimated_cost,
        )

    def charge(self, agent_id: str, task_id: str, actual_cost: float) -> GuardDecision:
        """Debit the budget for a completed call; veto once the budget is killed."""
        ok, reason, alerts = self._guard.check_and_charge(agent_id, task_id, actual_cost)
        budget = self._guard.get_budget(agent_id)
        alert_actions = [a.action.value for a in alerts]
        if not ok:
            return GuardDecision.block(
                "cost_limit_exceeded",
                reason,
                signals=["cost_limit_exceeded"],
                agent_id=agent_id,
                task_id=task_id,
                actual_cost=actual_cost,
                spent_today_usd=budget.spent_today_usd,
                throttled=budget.throttled,
                killed=budget.killed,
                alerts=alert_actions,
            )
        return GuardDecision.allow(
            reason=reason,
            agent_id=agent_id,
            task_id=task_id,
            actual_cost=actual_cost,
            spent_today_usd=budget.spent_today_usd,
            throttled=budget.throttled,
            killed=budget.killed,
            alerts=alert_actions,
        )
