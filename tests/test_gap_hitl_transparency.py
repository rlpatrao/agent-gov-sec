"""
Tests for the HITL + transparency guard wrappers.

Both exercise the real agent_os symbols (no upstream mocking) and complete in
well under three seconds — no real sleeps. The escalation approve path
schedules ``manager.approve(request_id)`` on a concurrent task before awaiting
``request_approval`` so it resolves immediately; the deny path uses a
zero-second timeout so the busy-wait loop exits at once.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_os.escalation import EscalationManager, EscalationPolicy

from governance.extensions.escalation_guard import HumanEscalationGuard
from governance.extensions.transparency_guard import TransparencyGuard


# --------------------------------------------------------------------------- #
# Escalation guard
# --------------------------------------------------------------------------- #


def test_escalation_pass_auto_approved():
    """An action not in the approval set requires no escalation and passes."""
    guard = HumanEscalationGuard(
        actions_requiring_approval=["deploy_prod"],
        timeout_seconds=5,
        default_on_timeout="deny",
    )

    assert guard.requires_approval("read_file") is False

    decision = asyncio.run(
        guard.approve_tool("agent-1", "read_file", {"path": "/etc/hosts"})
    )
    assert decision.allowed is True
    assert "escalation_not_required" in decision.signals


def test_escalation_intercept_timeout_deny():
    """A non-responding approver with default_on_timeout='deny' blocks."""
    # timeout_seconds=0 => deadline == now, busy-wait loop never iterates and
    # the request times out immediately (no real wall-clock wait).
    guard = HumanEscalationGuard(
        actions_requiring_approval=["deploy_prod"],
        timeout_seconds=0,
        default_on_timeout="deny",
    )

    assert guard.requires_approval("deploy_prod") is True

    decision = asyncio.run(
        guard.approve_tool("agent-1", "deploy_prod", {"target": "prod"})
    )
    assert decision.allowed is False
    assert decision.code == "escalation_denied"


def test_escalation_approve_via_concurrent_task():
    """A concurrent approver granting the request before timeout yields a pass."""

    captured: dict[str, str] = {}

    async def approval_handler(request):
        # request_approval invokes this synchronously (before busy-waiting),
        # so we capture the generated request_id without any sleep.
        captured["id"] = request.request_id

    policy = EscalationPolicy(
        actions_requiring_approval=["deploy_prod"],
        timeout_seconds=5,
        default_on_timeout="deny",
    )
    manager = EscalationManager(policy, approval_handler=approval_handler)
    guard = HumanEscalationGuard(manager=manager)

    async def scenario():
        async def approver():
            for _ in range(200):
                rid = captured.get("id")
                if rid:
                    manager.approve(rid, decided_by="oncall")
                    return
                await asyncio.sleep(0)
            raise AssertionError("approval_handler never captured a request id")

        task = asyncio.create_task(approver())
        decision = await guard.approve_tool(
            "agent-1", "deploy_prod", {"target": "prod"}
        )
        await task
        return decision

    decision = asyncio.run(scenario())
    assert decision.allowed is True
    assert "escalation_approved" in decision.signals
    assert decision.metadata["decided_by"] == "oncall"


# --------------------------------------------------------------------------- #
# Transparency guard
# --------------------------------------------------------------------------- #


def test_transparency_intercept_unconfirmed_blocks():
    """A fresh session that never confirmed disclosure is blocked."""
    guard = TransparencyGuard()

    decision = guard.check_tool("run-block", "query_db", {"q": "SELECT 1"})
    assert decision.allowed is False
    assert decision.code == "transparency_unconfirmed"


def test_transparency_pass_after_confirm():
    """Once the session confirms disclosure, the same call passes."""
    guard = TransparencyGuard()
    guard.confirm("run-pass")

    decision = guard.check_tool("run-pass", "query_db", {"q": "SELECT 1"})
    assert decision.allowed is True
    assert "transparency_confirmed" in decision.signals
    assert decision.metadata["ai_disclosure"] is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
