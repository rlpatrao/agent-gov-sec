"""
governance.extensions.reversibility_guard — a per-call guard wrapping
``agent_os.reversibility.ReversibilityChecker``.

The checker maps an action key to a :class:`ReversibilityLevel`
(``FULLY_REVERSIBLE`` … ``IRREVERSIBLE`` / ``UNKNOWN``) and reports whether the
action needs extra human approval. This wrapper exposes a pure
:meth:`check_action` that returns a :class:`GuardDecision` rather than raising,
so the pipeline stays the single place that maps a block onto a
``GovernanceViolation``. It does not import the pipeline and it is flag-agnostic.

Quirk workarounds applied:

* The checker is constructed with ``block_irreversible=True`` so
  ``should_block`` actually enforces (it returns ``False`` for everything when
  that flag is left at its default).
* ``should_block`` only fires for ``IRREVERSIBLE``. For ``UNKNOWN`` actions the
  checker returns ``requires_extra_approval=True`` but ``should_block`` is
  ``False`` — so the wrapper also gates on ``requires_extra_approval`` (when no
  approval is supplied) to avoid letting unmapped/unknown actions through under
  a hard-block posture.
* The tool name is used directly as the action key; a ``tool_action_map`` may be
  supplied to translate tool names onto the fixed reversibility vocabulary.
"""

from __future__ import annotations

from typing import Any

from agent_os.reversibility import ReversibilityChecker

from governance.extensions.decision import GuardDecision


class ReversibilityGuard:
    """Wraps a :class:`ReversibilityChecker` and returns a uniform verdict."""

    def __init__(
        self,
        tool_action_map: dict[str, str] | None = None,
        checker: ReversibilityChecker | None = None,
    ) -> None:
        # Hard-block posture: should_block is a no-op unless block_irreversible
        # was set at construction.
        self.checker = checker or ReversibilityChecker(block_irreversible=True)
        self.tool_action_map = dict(tool_action_map or {})

    def _action_for(self, name: str) -> str:
        return self.tool_action_map.get(name, name)

    def check_action(self, name: str, args: Any = None, approval: bool = False) -> GuardDecision:
        """Assess a tool's reversibility; block irreversible / approval-gated."""
        action = self._action_for(name)
        assessment = self.checker.assess(action)

        block_irreversible = self.checker.should_block(action)
        needs_approval = assessment.requires_extra_approval and not approval

        if block_irreversible or needs_approval:
            plan = self.checker.get_compensation_plan(action)
            comp = ", ".join(
                f"{c.action} (effectiveness={c.effectiveness})" for c in plan
            ) or "none"
            return GuardDecision.block(
                "irreversible_action",
                f"tool {name!r} (action {action!r}) is {assessment.level.value}; "
                f"{assessment.reason}; compensation: {comp}",
                signals=[assessment.level.value],
                level=assessment.level.value,
                requires_extra_approval=assessment.requires_extra_approval,
                approval=approval,
                compensating_actions=[c.action for c in plan],
            )

        return GuardDecision.allow(
            f"tool {name!r} (action {action!r}) is {assessment.level.value}; allowed",
            level=assessment.level.value,
        )
