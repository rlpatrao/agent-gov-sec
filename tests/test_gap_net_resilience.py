"""
Tests for the net-resilience guard wrappers (subgroup: net-resilience).

  - EgressGuard wraps agent_os.egress_policy.EgressPolicy (fail-closed deny).
  - CircuitBreakerGuard wraps agent_os.circuit_breaker.CircuitBreaker.

Each guard is exercised with both a PASS (allowed=True) and an INTERCEPT
(allowed=False + expected code) case drawn from the discovery demo_scenario. No
upstream mocking; the real agent_os symbols are driven. No wall-clock waits:
OPEN state is asserted directly after crossing the failure threshold.
"""

from __future__ import annotations

from governance.extensions.circuit_breaker_guard import CircuitBreakerGuard
from governance.extensions.egress_guard import EgressGuard


# --------------------------------------------------------------------------- #
# EgressGuard — demo_scenario: *.anthropic.com allow-listed, default deny.
# --------------------------------------------------------------------------- #


def test_egress_guard_pass_allowlisted_host() -> None:
    guard = EgressGuard()
    decision = guard.check_tool("http_get", {"url": "https://api.anthropic.com/v1/messages"})
    assert decision.allowed is True


def test_egress_guard_intercept_unlisted_host() -> None:
    guard = EgressGuard()
    decision = guard.check_tool("http_get", {"url": "https://evil-exfil.io/collect"})
    assert decision.allowed is False
    assert decision.code == "egress_denied"


def test_egress_guard_allows_non_network_tool() -> None:
    guard = EgressGuard()
    decision = guard.check_tool("read_file", {"path": "/etc/hosts"})
    assert decision.allowed is True


# --------------------------------------------------------------------------- #
# CircuitBreakerGuard — demo_scenario: threshold=2, success keeps CLOSED;
# two failures flip OPEN and the next allow_call is blocked.
# --------------------------------------------------------------------------- #


def test_circuit_breaker_pass_while_closed() -> None:
    guard = CircuitBreakerGuard(failure_threshold=2, recovery_timeout_seconds=30)
    # A healthy tool: success keeps the circuit CLOSED, allow_call permits it.
    guard.record_success("search_tool")
    decision = guard.allow_call("search_tool")
    assert decision.allowed is True


def test_circuit_breaker_intercept_when_open() -> None:
    guard = CircuitBreakerGuard(failure_threshold=2, recovery_timeout_seconds=30)
    # Flaky tool fails twice; the 2nd failure flips the breaker to OPEN. No
    # wall-clock wait — the OPEN state is asserted immediately.
    guard.record_failure("search_tool")
    guard.record_failure("search_tool")
    decision = guard.allow_call("search_tool")
    assert decision.allowed is False
    assert decision.code == "circuit_open"
    assert "search_tool" in decision.reason
