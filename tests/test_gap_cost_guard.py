"""Tests for governance.extensions.cost_guard.CostGuard.

Exercises the real ``agent_sre.cost.CostGuard`` symbol (no upstream mocking)
via the discovery demo_scenario: a within-budget PASS and an over-limit /
budget-killed INTERCEPT. No wall-clock sleeps.
"""

from __future__ import annotations

from governance.extensions.cost_guard import CostGuard


def _guard() -> CostGuard:
    # Demo scenario: per_task_limit=1.0, per_agent_daily_limit=2.0.
    return CostGuard(per_task_limit=1.0, per_agent_daily_limit=2.0)


def test_check_within_budget_passes() -> None:
    guard = _guard()
    decision = guard.check("a1", 0.5)
    assert decision.allowed is True
    assert decision.code == ""


def test_charge_within_budget_passes() -> None:
    guard = _guard()
    decision = guard.charge("a1", "t1", 0.5)
    assert decision.allowed is True
    assert decision.metadata["killed"] is False


def test_check_over_per_task_limit_intercepts() -> None:
    guard = _guard()
    decision = guard.check("a1", 5.0)
    assert decision.allowed is False
    assert decision.code == "cost_limit_exceeded"
    assert "exceeds per-task limit" in decision.reason
    assert "cost_limit_exceeded" in decision.signals


def test_charge_past_daily_limit_kills_and_intercepts() -> None:
    guard = _guard()
    # Four $0.5 charges consume the $2.0 daily budget; the fourth crosses the
    # kill_switch_threshold and marks the budget killed (all allowed so far).
    for i in range(4):
        guard.charge("a1", f"t{i}", 0.5)
    # A further charge is denied because the agent's budget is killed.
    decision = guard.charge("a1", "t5", 0.5)
    assert decision.allowed is False
    assert decision.code == "cost_limit_exceeded"
    assert decision.metadata["killed"] is True

    # The veto also surfaces on the pre-call check once killed.
    pre = guard.check("a1", 0.1)
    assert pre.allowed is False
    assert pre.code == "cost_limit_exceeded"
