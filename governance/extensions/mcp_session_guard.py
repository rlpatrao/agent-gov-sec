"""
governance.extensions.mcp_session_guard — establishes and validates the MCP
agent session identity the rest of the MCP guards key on.

Wraps ``agent_os.mcp_session_auth.MCPSessionAuthenticator``. ``create`` issues a
token at MCP-client connect time; ``validate`` is called before each tool
invocation and returns a ``GuardDecision``. A ``None`` from the authenticator
(wrong agent, forged/expired token, missing identity) is fail-closed and maps to
a block; the wrapper never raises ``GovernanceViolation``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from agent_os.mcp_session_auth import MCPSessionAuthenticator

from governance.extensions.decision import GuardDecision


class McpSessionGuard:
    """Issues and validates MCP session tokens.

    Concurrency quirk: ``create_session`` raises ``RuntimeError`` (not a bool)
    when ``max_concurrent_sessions`` is exceeded. ``create`` here lets that
    propagate — it is a connect-time wiring failure, not a per-call verdict — so
    callers can decide on retry/backoff. The per-call ``validate`` path is pure
    and fail-closed.
    """

    def __init__(
        self,
        *,
        session_ttl: timedelta = timedelta(hours=1),
        max_concurrent_sessions: int = 10,
        authenticator: Optional[MCPSessionAuthenticator] = None,
        session_store: Any = None,
    ) -> None:
        if authenticator is not None:
            self._auth = authenticator
        else:
            self._auth = MCPSessionAuthenticator(
                session_ttl=session_ttl,
                max_concurrent_sessions=max_concurrent_sessions,
                session_store=session_store,
            )

    def create(self, agent_id: str, user_id: Optional[str] = None) -> str:
        """Issue a session token for ``agent_id``. Connect-time, not per-call."""
        return self._auth.create_session(agent_id, user_id)

    def validate(self, agent_id: str, token: str) -> GuardDecision:
        """Validate ``token`` for ``agent_id``. None from the authenticator
        (wrong agent / forged / expired / empty) is fail-closed -> block."""
        session = self._auth.validate_session(agent_id, token)
        if session is None:
            return GuardDecision.block(
                "mcp_session_invalid",
                f"MCP session validation failed for agent '{agent_id}'",
                signals=["mcp_session_guard"],
                agent_id=agent_id,
            )
        return GuardDecision.allow(
            reason="MCP session valid",
            agent_id=agent_id,
            user_id=session.user_id,
            rate_limit_key=session.rate_limit_key,
        )
