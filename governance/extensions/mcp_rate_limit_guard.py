"""
governance.extensions.mcp_rate_limit_guard — per-call sliding-window rate limit
for MCP tool calls.

Wraps ``agent_os.mcp_sliding_rate_limiter.MCPSlidingRateLimiter``, which caps the
number of calls a given agent may make within a moving time window
(``max_calls_per_window`` over ``window_size`` seconds). It maps onto the
BEFORE_TOOL hook: ``allow(agent_id)`` calls ``try_acquire``, which CONSUMES one
unit of budget on success.

``allow(agent_id)`` returns a ``GuardDecision``:
  - allowed when ``try_acquire`` returns True (budget remained in the window);
  - blocked with ``code='mcp_rate_limited'`` when it returns False, or when
    ``try_acquire`` raises (see fail-closed note below).

Quirks honored (per discovery notes):
  - Unlike the gateway, this module does NOT wrap ``try_acquire`` in a broad
    try/except — a bad store/clock or an empty ``agent_id`` (which raises
    ``ValueError``) propagates out. To stay consistent with the rest of the
    fail-closed suite, the wrapper catches any exception and treats it as a
    block rather than letting traffic through.
  - ``try_acquire`` consumes budget by appending a timestamp; the read-only
    accessors prune-and-read only. The wrapper relies solely on ``try_acquire``.
  - This complements the gateway's integer-counter budget; the two keep separate
    stores (timestamp buckets here vs. an integer counter in the gateway).

The wrapper is flag-agnostic and never imports the pipeline; the pipeline gates
it behind ``GALAXY_GAP_MCP_RATE_LIMIT`` and maps a block onto GovernanceViolation.
"""

from __future__ import annotations

from typing import Callable, Optional

from agent_os.mcp_sliding_rate_limiter import MCPSlidingRateLimiter

from governance.extensions.decision import GuardDecision


class McpRateLimitGuard:
    """Holds one sliding-window rate limiter and admits calls within the window."""

    def __init__(
        self,
        *,
        max_calls_per_window: int = 100,
        window_size: float = 300.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        kwargs: dict = {
            "max_calls_per_window": max_calls_per_window,
            "window_size": window_size,
        }
        if clock is not None:
            kwargs["clock"] = clock
        self._limiter = MCPSlidingRateLimiter(**kwargs)

    def allow(self, agent_id: str) -> GuardDecision:
        """Return a GuardDecision for a tool call; consumes one unit of budget on allow."""
        try:
            acquired = self._limiter.try_acquire(agent_id)
        except Exception as exc:  # noqa: BLE001 — fail closed to match the suite
            return GuardDecision.block(
                "mcp_rate_limited",
                f"rate-limit check failed for agent {agent_id!r}: {exc}",
                signals=["fail_closed"],
                agent_id=agent_id,
            )

        if acquired:
            return GuardDecision.allow(
                reason=f"agent {agent_id!r} within sliding-window budget",
                agent_id=agent_id,
            )

        return GuardDecision.block(
            "mcp_rate_limited",
            f"agent {agent_id!r} exceeded sliding-window call budget",
            signals=["window_exceeded"],
            agent_id=agent_id,
        )
