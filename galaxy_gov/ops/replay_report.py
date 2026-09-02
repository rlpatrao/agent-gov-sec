"""Golden-trace regression report for the reliability demo section.

Operational regression harness (flag: GALAXY_OPS_REPLAY_GOLDEN). Drives a small
``GoldenTraceSuite`` through ``GoldenTraceManager.run_suite`` against a callable
agent, for a matching output and a regressed output, surfacing ``ci_passed`` —
the gate a CI step would fail on.

Output similarity is computed with ``difflib.SequenceMatcher`` against the
golden ``tolerance``. ``run_suite`` calls ``agent_fn`` with the trace dict and
expects an output string; with ``tolerance=0.0`` only an exact match passes, so
a changed answer drops ``pass_rate`` below ``pass_threshold`` and ``ci_passed``
becomes False. ``GoldenTraceSuite`` / ``GoldenTrace`` are keyword-only pydantic
models.
"""

from __future__ import annotations

from typing import Any

from agent_sre.replay import GoldenTrace, GoldenTraceSuite
from agent_sre.replay.golden_manager import GoldenTraceManager


def _build_suite() -> GoldenTraceSuite:
    return GoldenTraceSuite(
        name="smoke",
        traces=[
            GoldenTrace(
                name="capital",
                description="Frozen answer for the capital-of-France prompt.",
                trace={"input": {"query": "capital of France"}},
                expected_output="Paris",
                tolerance=0.0,
            )
        ],
        pass_threshold=0.95,
    )


def _run(agent_fn: Any) -> dict[str, Any]:
    suite = _build_suite()
    result = GoldenTraceManager().run_suite(suite, agent_fn)
    return {
        "suite_name": result.suite_name,
        "total": result.total,
        "passed": result.passed,
        "failed": result.failed,
        "pass_rate": result.pass_rate,
        "ci_passed": result.ci_passed,
    }


def run_replay_demo() -> dict[str, Any]:
    """Run the golden suite against a matching agent and a regressed agent.

    PASS: the agent returns the frozen ``"Paris"`` -> ``pass_rate`` 1.0,
    ``ci_passed`` True. INTERCEPT/regression: the agent returns ``"London"`` ->
    the trace fails the tolerance check, ``pass_rate`` drops below the
    threshold, ``ci_passed`` is False.
    """
    matching = _run(lambda trace: "Paris")
    regressed = _run(lambda trace: "London")

    return {
        "suite_name": "smoke",
        "pass_threshold": 0.95,
        "pass": matching,
        "intercept": regressed,
        "regression_detected": not regressed["ci_passed"],
    }
