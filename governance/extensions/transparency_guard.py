"""
governance.extensions.transparency_guard — AI-disclosure confirmation guard.

Wraps ``agent_os.transparency.TransparencyInterceptor`` so a tool call is held
until the session has confirmed the AI-disclosure notice (EU AI Act Art. 50
transparency obligation). The guard builds a ``ToolCallRequest`` shim and calls
``intercept``; an ``allowed=False`` result is mapped to a block with code
``transparency_unconfirmed``. When allowed, the Art. 50 disclosure text from the
result's audit entry is carried through on the verdict metadata.

The session id is read from ``metadata['session_id']`` in the interceptor,
falling back to ``agent_id`` then ``'default'``. The guard passes a stable
per-run session id so calls do not collapse onto the shared ``'default'``
session. ``confirm(session_id)`` proxies the interceptor's
``confirm_disclosure`` so the run-start wiring can mark a session confirmed.

The module does not import the pipeline; it returns a verdict.
"""

from __future__ import annotations

from typing import Any

from agent_os.transparency import (
    ToolCallRequest,
    TransparencyInterceptor,
    TransparencyLevel,
)

from governance.extensions.decision import GuardDecision

BLOCK_CODE = "transparency_unconfirmed"


class TransparencyGuard:
    """Block tool calls until the session confirms the AI-disclosure notice.

    Built once with a ``TransparencyInterceptor`` configured to require
    disclosure confirmation. ``check_tool`` is a pure method returning a
    :class:`GuardDecision`; ``confirm`` marks a session confirmed so the PASS
    path is reachable.
    """

    def __init__(
        self,
        interceptor: TransparencyInterceptor | None = None,
        *,
        default_level: TransparencyLevel = TransparencyLevel.ENHANCED,
    ) -> None:
        if interceptor is None:
            interceptor = TransparencyInterceptor(
                default_level=default_level,
                require_disclosure_confirmation=True,
            )
        self._interceptor = interceptor
        self._default_level = default_level

    @property
    def interceptor(self) -> TransparencyInterceptor:
        return self._interceptor

    def confirm(self, session_id: str) -> None:
        """Mark ``session_id`` as having confirmed the AI-disclosure notice.

        Call once per session at run start so subsequent tool calls in that
        session pass.
        """
        self._interceptor.confirm_disclosure(session_id)

    def check_tool(
        self, session_id: str, name: str, args: dict[str, Any]
    ) -> GuardDecision:
        """Screen a tool call against the session's disclosure-confirmation state.

        Returns a block with ``transparency_unconfirmed`` when the interceptor
        denies the call; otherwise allows and carries the Art. 50 disclosure
        text from the audit entry on the verdict metadata.
        """
        request = ToolCallRequest(
            tool_name=name,
            arguments=args,
            agent_id=session_id,
            metadata={
                "session_id": session_id,
                "transparency_level": self._default_level,
            },
        )
        result = self._interceptor.intercept(request)

        if not result.allowed:
            return GuardDecision.block(
                BLOCK_CODE,
                result.reason or "AI disclosure must be confirmed before tool execution",
                signals=["transparency_unconfirmed"],
                session_id=session_id,
            )

        audit = result.audit_entry or {}
        return GuardDecision(
            allowed=True,
            reason="AI disclosure confirmed for session",
            signals=["transparency_confirmed"],
            metadata={
                "session_id": session_id,
                "ai_disclosure": audit.get("_ai_disclosure"),
            },
        )
