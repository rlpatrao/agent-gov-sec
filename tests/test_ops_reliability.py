"""Tests for the ops-reliability operational reports.

Each report runs the real agent_sre symbol (no upstream mocking) and exposes a
PASS window and an INTERCEPT/failure window; the assertions check both.
"""

from __future__ import annotations

from governance.ops.accuracy_report import run_accuracy_demo
from governance.ops.evals_report import run_evals_demo
from governance.ops.replay_report import run_replay_demo
from governance.ops.slo_report import run_slo_demo


def test_slo_report_pass_and_burn() -> None:
    report = run_slo_demo()

    # PASS window: healthy, no firing alerts, burn rate at zero.
    assert report["pass"]["status"] == "healthy"
    assert report["pass"]["firing_alerts"] == []
    assert report["pass"]["burn_rate"] == 0.0
    assert report["pass"]["compliance"] == 1.0

    # INTERCEPT window: budget actually fires above the critical burn rate.
    assert report["budget_fires"] is True
    assert report["intercept"]["firing_alerts"]
    assert report["intercept"]["critical_firing"] is True
    assert report["intercept"]["burn_rate"] > report["burn_rate_critical_threshold"]
    assert report["intercept"]["status"] != "healthy"


def test_accuracy_report_compliant_and_breach() -> None:
    report = run_accuracy_demo()

    assert report["risk_tier"] == "high"
    assert "tool_call_accuracy" in report["declared_metrics"]

    # PASS: declared metric at/above its minimum is flagged compliant.
    assert report["pass"]["compliant"] is True
    assert "COMPLIANT" in report["pass"]["message"]

    # INTERCEPT: sub-threshold declared metric is flagged non-compliant.
    assert report["intercept"]["compliant"] is False
    assert "NON-COMPLIANT" in report["intercept"]["message"]

    # Documented fail-open: an undeclared metric passes silently.
    assert report["undeclared_fail_open"]["compliant"] is True


def test_evals_report_pass_and_fail() -> None:
    report = run_evals_demo()

    # PASS: benign answer clears the SAFETY suite.
    assert report["pass"]["overall_pass"] is True
    pass_safety = next(
        v for v in report["pass"]["verdicts"] if v["criterion"] == "safety"
    )
    assert pass_safety["verdict"] == "pass"

    # INTERCEPT: required SAFETY criterion fails -> overall_pass False.
    assert report["intercept"]["overall_pass"] is False
    intercept_safety = next(
        v for v in report["intercept"]["verdicts"] if v["criterion"] == "safety"
    )
    assert intercept_safety["verdict"] == "fail"


def test_replay_report_match_and_regression() -> None:
    report = run_replay_demo()

    # PASS: matching output -> full pass rate, CI green.
    assert report["pass"]["pass_rate"] == 1.0
    assert report["pass"]["ci_passed"] is True

    # INTERCEPT: regressed output -> CI fails, regression detected.
    assert report["intercept"]["ci_passed"] is False
    assert report["intercept"]["pass_rate"] < report["pass_threshold"]
    assert report["regression_detected"] is True
