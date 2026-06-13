"""
governance.extensions.circuit_breaker_guard — per-tool circuit-breaker guard.

Wraps ``agent_os.circuit_breaker.CircuitBreaker`` (the back-compat re-export of
the canonical ``agent_sre.cascade.circuit_breaker`` symbols). The guard keeps one
``CircuitBreaker`` per tool name. ``allow_call(name)`` consults that breaker's
state: while CLOSED (and during the single HALF_OPEN trial) the call is allowed;
once the configured ``failure_threshold`` is crossed the breaker flips OPEN and
``allow_call`` returns ``GuardDecision(allowed=False, code='circuit_open')`` with
the ``CircuitOpenError`` message as the reason. The pipeline brackets dispatch
with ``record_success(name)`` / ``record_failure(name)``.

Quirks honored (per discovery notes):
  - ``CircuitBreakerConfig`` accepts both ``recovery_timeout_seconds`` and the
    legacy ``reset_timeout_seconds`` alias and raises if both are given with
    different values, so the constructor passes only ``recovery_timeout_seconds``.
  - ``CircuitBreaker.__init__`` accepts a positional ``CircuitBreakerConfig`` in
    the ``agent_id`` slot for legacy callers; the wrapper uses the keyword form
    ``CircuitBreaker(agent_id, config=...)`` to avoid that ambiguity.
  - Reading state via ``get_state()`` can transition OPEN->HALF_OPEN once the
    recovery timeout elapses; the wrapper reads state once per call.

Flag-agnostic; the pipeline gates it behind ``GALAXY_GAP_CIRCUIT_BREAKER`` and
maps a block onto GovernanceViolation.
"""

from __future__ import annotations

from typing import Optional

from agent_os.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
)

from governance.extensions.decision import GuardDecision


class CircuitBreakerGuard:
    """Holds a per-tool CircuitBreaker registry and reports OPEN circuits."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        # Pass only recovery_timeout_seconds (never the reset_timeout_seconds
        # alias) to avoid the dual-alias ValueError.
        self._config = CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
            half_open_max_calls=half_open_max_calls,
        )
        self._breakers: dict[str, CircuitBreaker] = {}

    def _breaker(self, name: str) -> CircuitBreaker:
        breaker = self._breakers.get(name)
        if breaker is None:
            # Keyword config form; the agent_id slot carries the tool name.
            breaker = CircuitBreaker(name, config=self._config)
            self._breakers[name] = breaker
        return breaker

    def allow_call(self, name: str) -> GuardDecision:
        """Return a GuardDecision based on the tool's circuit state."""
        breaker = self._breaker(name)
        state = breaker.get_state()
        if state == CircuitState.OPEN:
            retry_after = float(self._config.recovery_timeout_seconds or 0.0)
            reason = str(CircuitOpenError(name, retry_after))
            return GuardDecision.block(
                "circuit_open",
                reason,
                signals=["circuit_open"],
                tool=name,
                state=state.value,
            )
        return GuardDecision.allow(
            reason=f"circuit for tool {name!r} is {state.value}",
            tool=name,
            state=state.value,
        )

    def record_success(self, name: str) -> None:
        self._breaker(name).record_success()

    def record_failure(self, name: str) -> None:
        self._breaker(name).record_failure()

    def get_state(self, name: str) -> Optional[str]:
        breaker = self._breakers.get(name)
        return breaker.get_state().value if breaker is not None else None
