"""SLO / error-budget report for the reliability demo section.

Operational reliability artifact (flag: GALAXY_OPS_SLO_BUDGET). Builds an
``SLO`` over a success-rate ``SLI`` with an ``ErrorBudget``, records a burst of
good events and then a burst with a bad fraction, and reports the evaluated
status plus the burn rate and any firing alerts. This is fleet-aggregate
reporting fed by recorded events over a window, not a per-call gate.

``SLI`` is abstract (it requires ``collect()``); a minimal concrete
``_SuccessRateSLI`` is defined here whose ``collect()`` returns the current
windowed value. ``SLI.current_value()`` / ``compliance()`` return ``None`` when
no measurements exist (absence of data reads as "no signal", not a breach), so
the demo records measurements before evaluating to exercise a real status.
"""

from __future__ import annotations

from typing import Any

from agent_sre.slo import SLI, SLO, ErrorBudget, SLIValue


class _SuccessRateSLI(SLI):
    """Concrete success-rate SLI.

    ``SLI`` is an abstract base requiring ``collect()``; this implementation
    returns an ``SLIValue`` carrying the current windowed average so the SLI can
    be instantiated and driven by ``record()``.
    """

    def collect(self) -> SLIValue:
        return SLIValue(name=self.name, value=self.current_value() or 0.0)


def _evaluate_window(
    *,
    bad_every: int | None,
    event_count: int,
) -> dict[str, Any]:
    """Build a fresh SLO, drive ``event_count`` events, and report the outcome.

    When ``bad_every`` is ``None`` every event is good (the PASS window). When it
    is an integer N, every Nth event is recorded as bad (the burn window). The
    SLI records a per-event measurement so ``evaluate()`` resolves to a concrete
    status rather than ``UNKNOWN``.
    """
    sli = _SuccessRateSLI("success_rate", target=99.0, window="30d")
    budget = ErrorBudget(
        total=0.01,
        burn_rate_alert=2.0,
        burn_rate_critical=10.0,
    )
    slo = SLO("answer-quality", [sli], error_budget=budget)

    bad_events = 0
    for i in range(event_count):
        bad = bad_every is not None and (i % bad_every == 0)
        if bad:
            bad_events += 1
        sli.record(50.0 if bad else 100.0)
        slo.record_event(good=not bad)

    status = slo.evaluate()
    burn_rate = budget.burn_rate()
    alerts = budget.firing_alerts()

    return {
        "status": status.value,
        "burn_rate": burn_rate,
        "firing_alerts": [
            {"name": a.name, "severity": a.severity, "rate": a.rate} for a in alerts
        ],
        "critical_firing": any(a.severity == "critical" for a in alerts),
        "compliance": sli.compliance(),
        "bad_events": bad_events,
        "exhaustion_action": budget.exhaustion_action.value,
    }


def run_slo_demo() -> dict[str, Any]:
    """Evaluate a PASS window and a budget-burning window.

    PASS: 100 good events -> a healthy status, burn rate near zero, no firing
    alerts. INTERCEPT: a 100-event burst with 20% bad events drives the burn
    rate above ``burn_rate_critical`` so ``firing_alerts()`` is non-empty and the
    status is no longer healthy.
    """
    healthy = _evaluate_window(bad_every=None, event_count=100)
    burning = _evaluate_window(bad_every=5, event_count=100)

    return {
        "slo_name": "answer-quality",
        "indicator": "success_rate",
        "burn_rate_critical_threshold": 10.0,
        "pass": healthy,
        "intercept": burning,
        "budget_fires": bool(burning["firing_alerts"]),
    }
