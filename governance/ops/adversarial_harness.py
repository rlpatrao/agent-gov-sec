"""Adversarial (red-team) evaluation harness for the supply-chain demo section.

Operational pre-deployment / CI stress test (flag: GALAXY_GAP_ADVERSARIAL_EVAL).
Runs the built-in attack vectors through a governance interceptor and reports a
defense rate (1 - risk_score) plus remediation recommendations. It validates the
before_tool capability / blocked-pattern guards rather than running inline on
live traffic.

The interceptor contract is duck-typed: any object exposing
``intercept(ToolCallRequest) -> obj`` where ``obj`` has ``.allowed`` (bool) and
``.reason`` works. ``DefaultInterceptor`` is a self-contained adapter for tests
and demos that returns ``GuardDecision`` directly (a GuardDecision has both
``.allowed`` and ``.reason``), so it satisfies the contract without importing
the pipeline. The pipeline-backed adapter is wired separately in the demo.

Critical: the adapter must map a governance rejection to ``allowed=False``. An
adapter that swallows exceptions and returns ``allowed=True`` would make every
vector falsely pass and read a 0.0 risk score.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from agent_os.adversarial import AdversarialEvaluator, BUILTIN_VECTORS

from governance.extensions.decision import GuardDecision


# Tool names that must never be reachable through governed tool calls.
DEFAULT_BLOCKED_TOOLS = frozenset({"shell_exec", "file_access"})

# Argument-value substrings that signal prompt-injection or policy override.
DEFAULT_BLOCKED_PATTERNS = (
    "ignore all previous instructions",
    "reveal secrets",
    "disable_governance",
    "rm -rf",
    "base64decode",
    "grant admin",
    "sudo access",
)


class DefaultInterceptor:
    """A small, self-contained governance interceptor for the harness.

    Built once. ``intercept`` is pure and returns a ``GuardDecision``: it blocks
    requests whose tool name is on the blocked-tool set, and requests whose
    argument values contain a blocked pattern (including a few override/escalation
    signals carried as argument keys). Everything else is allowed.
    """

    def __init__(
        self,
        blocked_tools: Sequence[str] | None = None,
        blocked_patterns: Sequence[str] | None = None,
    ) -> None:
        self._blocked_tools = frozenset(
            blocked_tools if blocked_tools is not None else DEFAULT_BLOCKED_TOOLS
        )
        self._blocked_patterns = tuple(
            p.lower()
            for p in (
                blocked_patterns
                if blocked_patterns is not None
                else DEFAULT_BLOCKED_PATTERNS
            )
        )

    def _flatten(self, arguments: dict[str, Any]) -> str:
        parts: list[str] = []
        for key, value in arguments.items():
            parts.append(str(key))
            parts.append(str(value))
        return " ".join(parts).lower()

    def intercept(self, request: Any) -> GuardDecision:
        tool_name = getattr(request, "tool_name", "")
        arguments = getattr(request, "arguments", {}) or {}

        if tool_name in self._blocked_tools:
            return GuardDecision(
                allowed=False,
                code="ADVERSARIAL_TOOL_BLOCKED",
                reason=f"tool '{tool_name}' is not on the allow-list",
            )

        haystack = self._flatten(arguments)
        for pattern in self._blocked_patterns:
            if pattern in haystack:
                return GuardDecision(
                    allowed=False,
                    code="ADVERSARIAL_PATTERN_BLOCKED",
                    reason=f"blocked pattern detected: '{pattern}'",
                )

        # Treat explicit governance-override / escalation flags as a block even
        # if their textual value did not match a pattern.
        if arguments.get("disable_governance") or arguments.get("override"):
            return GuardDecision(
                allowed=False,
                code="ADVERSARIAL_OVERRIDE_BLOCKED",
                reason="governance/override flag present in tool arguments",
            )

        return GuardDecision(allowed=True, code="", reason="permitted")


def run_adversarial(
    interceptor: Any | None = None,
    vectors: Optional[Sequence[Any]] = None,
) -> dict[str, Any]:
    """Run the adversarial evaluation and return a defense-rate report.

    Args:
        interceptor: An object exposing ``intercept(ToolCallRequest)`` returning
            something with ``.allowed`` / ``.reason``. Defaults to
            ``DefaultInterceptor()``.
        vectors: Optional vector list; defaults to ``BUILTIN_VECTORS``.

    Returns a report dict with totals, ``risk_score``, the derived
    ``defense_rate`` (1 - risk_score), recommendations, and per-vector outcomes.
    """
    if interceptor is None:
        interceptor = DefaultInterceptor()

    evaluator = AdversarialEvaluator(interceptor)
    report = evaluator.evaluate(vectors)

    defense_rate = 1.0 - report.risk_score

    return {
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "risk_score": report.risk_score,
        "defense_rate": defense_rate,
        "recommendations": list(report.recommendations),
        "results": [
            {
                "name": r.vector.name,
                "category": r.vector.category.value,
                "tool_name": r.vector.tool_name,
                "expected": r.vector.expected_outcome,
                "actual": r.actual_outcome,
                "passed": r.passed,
                "reason": r.reason,
            }
            for r in report.results
        ],
    }
