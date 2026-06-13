"""
governance.extensions.semantic_policy_guard — a per-call guard wrapping
``agent_os.semantic_policy.SemanticPolicyEngine``.

The engine is a semantic upgrade of the brittle blocked-substring scan: it
classifies a tool invocation into an :class:`IntentCategory` with a confidence
score, and ``check`` raises :class:`PolicyDenied` when the classified category
is in the configured deny set above the confidence threshold.

This wrapper exposes a pure :meth:`check_tool` that returns a
:class:`GuardDecision` rather than raising, so the pipeline remains the single
place that maps a block onto a ``GovernanceViolation``. It does not import the
pipeline and it is flag-agnostic; the pipeline gates it.

Two block paths are combined:

* ``engine.check`` enforces the explicit deny set
  (``DESTRUCTIVE_DATA``, ``PRIVILEGE_ESCALATION``) and raises ``PolicyDenied``;
  the wrapper catches it and blocks with the classified category as the code.
* ``engine.classify`` (which never raises) is also consulted so the broader
  ``is_dangerous`` set — which includes ``SYSTEM_MODIFICATION`` (e.g. an
  ``rm -rf`` recursive force delete) and ``CODE_EXECUTION`` — is intercepted
  even though those categories sit outside the narrow deny set.

Quirk workarounds applied in the constructor:

* The built-in sample signals emit a ``UserWarning`` when the engine is built
  with no config and no custom signals. That warning is suppressed locally at
  construction time so importing the guard does not spam the audit log; the
  fact that sample signals are in use is recorded on ``self.using_sample_signals``.
"""

from __future__ import annotations

import warnings
from typing import Any

from agent_os.semantic_policy import (
    IntentCategory,
    PolicyDenied,
    SemanticPolicyEngine,
)

from governance.extensions.decision import GuardDecision

# Categories the engine enforces via the deny set (check raises PolicyDenied).
_DENY = [IntentCategory.DESTRUCTIVE_DATA, IntentCategory.PRIVILEGE_ESCALATION]


class SemanticPolicyGuard:
    """Wraps a :class:`SemanticPolicyEngine` and returns a uniform verdict."""

    def __init__(
        self,
        deny: list[IntentCategory] | None = None,
        confidence_threshold: float = 0.5,
        engine: SemanticPolicyEngine | None = None,
    ) -> None:
        self.deny = list(deny) if deny is not None else list(_DENY)
        self.confidence_threshold = confidence_threshold
        self.using_sample_signals = False
        if engine is not None:
            self.engine = engine
        else:
            # The built-in sample signals warn at construction; suppress the
            # noise but remember that sample signals are in effect.
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.engine = SemanticPolicyEngine(
                    deny=self.deny,
                    confidence_threshold=self.confidence_threshold,
                )
            self.using_sample_signals = any(
                issubclass(w.category, UserWarning) for w in caught
            )

    @staticmethod
    def _as_params(args: Any) -> dict[str, Any]:
        """Coerce the tool args into the dict shape the engine expects."""
        if isinstance(args, dict):
            return args
        return {"args": args}

    def check_tool(self, name: str, args: Any) -> GuardDecision:
        """Classify a tool call; block denied or otherwise-dangerous intent."""
        params = self._as_params(args)

        # Path 1: the deny set, enforced by the engine itself.
        try:
            classification = self.engine.check(name, params)
        except PolicyDenied as exc:
            cls = exc.classification
            return GuardDecision.block(
                "semantic_policy_denied",
                f"tool {name!r} classified as {cls.category.value} "
                f"(confidence {cls.confidence:.2f}); denied by policy",
                signals=list(cls.matched_signals),
                category=cls.category.value,
                confidence=cls.confidence,
            )

        # Path 2: the broader dangerous set (SYSTEM_MODIFICATION, CODE_EXECUTION,
        # DATA_EXFILTRATION) that classify flags via is_dangerous but that sits
        # outside the narrow deny set check enforces.
        if classification.is_dangerous:
            return GuardDecision.block(
                "semantic_policy_denied",
                f"tool {name!r} classified as {classification.category.value} "
                f"(confidence {classification.confidence:.2f}); dangerous intent",
                signals=list(classification.matched_signals),
                category=classification.category.value,
                confidence=classification.confidence,
            )

        return GuardDecision.allow(
            f"tool {name!r} classified as {classification.category.value} "
            f"(confidence {classification.confidence:.2f}); allowed",
            category=classification.category.value,
            confidence=classification.confidence,
        )
