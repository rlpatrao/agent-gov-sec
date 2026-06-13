"""
governance.extensions.memory_guard — a per-call guard wrapping
``agent_os.memory_guard.MemoryGuard``.

``MemoryGuard.validate_write`` screens content destined for a memory / RAG /
knowledge store for memory-poisoning signals (injection patterns, embedded code,
unicode manipulation, excessive special characters). It returns a
``ValidationResult`` whose ``allowed`` is ``False`` only when a HIGH or CRITICAL
alert is present.

This wrapper exposes a pure :meth:`check_write` that returns a
:class:`GuardDecision` rather than raising, so the pipeline stays the single
place that maps a block onto a ``GovernanceViolation``. It does not import the
pipeline and it is flag-agnostic.

Quirk workarounds applied:

* A single :class:`MemoryGuard` instance is held so its internal ``audit_log``
  accumulates across calls (exposed via :attr:`audit_log`).
* ``validate_write`` is fail-closed: if validation itself raises, the module
  appends a CRITICAL alert and returns ``allowed=False`` — that behavior is
  preserved (the wrapper does not swallow it).
* The content string is pulled from common tool-arg keys (``content``, ``text``,
  ``document``, ``value``) and the source defaults to the agent id, matching the
  ``validate_write(content, source=agent_id)`` seam.
"""

from __future__ import annotations

from typing import Any

from agent_os.memory_guard import MemoryGuard

from governance.extensions.decision import GuardDecision

_CONTENT_KEYS = ("content", "text", "document", "value", "body")


class MemoryWriteGuard:
    """Wraps a :class:`MemoryGuard` and returns a uniform verdict."""

    def __init__(self, guard: MemoryGuard | None = None) -> None:
        # Single instance so audit_log accumulates across writes.
        self.guard = guard or MemoryGuard()

    @property
    def audit_log(self) -> list[Any]:
        return self.guard.audit_log

    @staticmethod
    def _content_of(args: Any) -> str:
        if isinstance(args, dict):
            for key in _CONTENT_KEYS:
                if key in args and args[key] is not None:
                    return str(args[key])
            return ""
        return "" if args is None else str(args)

    def check_write(
        self,
        name: str,
        args: Any,
        source: str | None = None,
    ) -> GuardDecision:
        """Screen a memory-write tool call for poisoning signals."""
        content = self._content_of(args)
        src = source or (args.get("source") if isinstance(args, dict) else None) or "agent"

        result = self.guard.validate_write(content, source=src)

        if not result.allowed:
            alert_kinds = [a.alert_type.name for a in result.alerts]
            top = ", ".join(
                f"{a.alert_type.name}/{a.severity.name}" for a in result.alerts
            ) or "unspecified"
            return GuardDecision.block(
                "memory_poisoning",
                f"memory write via tool {name!r} from source {src!r} rejected; "
                f"alerts: {top}",
                signals=alert_kinds,
                source=src,
                alerts=[
                    {"type": a.alert_type.name, "severity": a.severity.name, "message": a.message}
                    for a in result.alerts
                ],
            )

        return GuardDecision.allow(
            f"memory write via tool {name!r} from source {src!r} accepted",
            source=src,
        )
