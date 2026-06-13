"""
governance.extensions.decision — the uniform verdict the sweep-era guard
wrappers return.

Every guard wrapper added under the full-sweep effort exposes pure methods that
return a ``GuardDecision`` rather than raising. Keeping the wrappers free of any
``GuardPipeline`` import (a) avoids a circular dependency (the pipeline imports
the wrappers, not the other way round) and (b) lets each wrapper be unit-tested
in isolation without standing up a pipeline. The pipeline is the single place
that maps ``GuardDecision(allowed=False)`` onto a ``GovernanceViolation`` at the
hook seam, so the block-vs-audit policy stays in one location.

``output`` carries the (possibly redacted/masked) text for output-mutating
guards — output PII redaction and content-quality gating populate it; pure
allow/deny guards leave it ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class GuardDecision:
    """Uniform result of a guard wrapper's evaluation.

    allowed   — False means the pipeline raises ``GovernanceViolation(code, reason)``.
    code      — machine code surfaced on a block (e.g. ``egress_denied``); the demo
                asserts on it.
    reason    — human-readable explanation, logged to the audit trail.
    signals   — short tags for telemetry (e.g. ``["default_deny"]``).
    metadata  — structured detail merged into the audit entry.
    output    — for output-mutating guards: the text to forward downstream
                (redacted/masked). ``None`` for allow/deny-only guards.
    """

    allowed: bool
    code: str = ""
    reason: str = ""
    signals: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    output: Optional[str] = None

    @classmethod
    def allow(cls, reason: str = "", **meta: Any) -> "GuardDecision":
        return cls(allowed=True, reason=reason, metadata=dict(meta))

    @classmethod
    def block(cls, code: str, reason: str, *, signals: Optional[list[str]] = None, **meta: Any) -> "GuardDecision":
        return cls(allowed=False, code=code, reason=reason, signals=signals or [], metadata=dict(meta))
