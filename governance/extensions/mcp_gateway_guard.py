"""
governance.extensions.mcp_gateway_guard — per-call MCP tool-call admission via
the aggregate MCP gateway.

Wraps ``agent_os.mcp_gateway.MCPGateway`` built from a ``GovernancePolicy``
(``agent_os.integrations.base.GovernancePolicy``). The gateway is the aggregate
outbound guard: it enforces the allow-list, the deny-list, built-in dangerous
patterns (SSN, credit card, shell metacharacters), and a per-agent integer call
budget (``max_tool_calls``). It maps onto the BEFORE_TOOL hook via
``intercept_tool_call(agent_id, tool_name, params) -> (bool, reason)``.

``check_tool(agent_id, name, args)`` returns a ``GuardDecision``:
  - allowed when ``intercept_tool_call`` returns ``(True, reason)``;
  - blocked with ``code='mcp_tool_denied'`` and the gateway's reason otherwise.

Quirks honored (per discovery notes):
  - The gateway is fail-closed everywhere: ``intercept_tool_call`` wraps its work
    in try/except and returns ``(False, 'Internal gateway error …')`` on any
    error, so a False verdict is always a block.
  - Budget is consumed atomically only on a successful allow, so denied calls do
    not burn budget — the wrapper does not need to compensate.
  - The gateway owns its own integer-counter rate-limit store, distinct from the
    timestamp buckets used by ``MCPSlidingRateLimiter``; the two are wired with
    separate stores and not shared.

The wrapper is flag-agnostic and never imports the pipeline; the pipeline gates
it behind ``GALAXY_GAP_MCP_GATEWAY`` and maps a block onto GovernanceViolation.
"""

from __future__ import annotations

from typing import Any, Optional

from agent_os.integrations.base import GovernancePolicy
from agent_os.mcp_gateway import MCPGateway

from governance.extensions.decision import GuardDecision


class McpGatewayGuard:
    """Builds one MCPGateway and admits outbound tool calls through it."""

    def __init__(
        self,
        *,
        allowed_tools: Optional[list[str]] = None,
        denied_tools: Optional[list[str]] = None,
        max_tool_calls: int = 10,
        policy: Optional[GovernancePolicy] = None,
    ) -> None:
        if policy is None:
            policy = GovernancePolicy(
                allowed_tools=list(allowed_tools or []),
                max_tool_calls=max_tool_calls,
            )
        self._gateway = MCPGateway(policy, denied_tools=list(denied_tools or []))

    def check_tool(self, agent_id: str, name: str, args: dict[str, Any]) -> GuardDecision:
        """Return a GuardDecision for an outbound tool call via the gateway."""
        params = args if isinstance(args, dict) else {"args": args}
        allowed, reason = self._gateway.intercept_tool_call(agent_id, name, params)

        if allowed:
            return GuardDecision.allow(reason=reason, tool=name, agent_id=agent_id)

        return GuardDecision.block(
            "mcp_tool_denied",
            reason,
            signals=["gateway_deny"],
            tool=name,
            agent_id=agent_id,
        )
