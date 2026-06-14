"""Accuracy-declaration compliance report for the reliability demo section.

Operational / compliance attestation (flag: GALAXY_OPS_ACCURACY_DECL). Builds a
HIGH-risk ``AccuracyDeclaration`` and validates declared metrics against
supplied SLI values, producing an EU-AI-Act-style compliance section.

Fail-open quirk: ``validate_against_sli`` on an UNDECLARED metric returns
``(True, '... no declared threshold (unconstrained)')`` — an unknown metric
passes silently. The PASS and INTERCEPT cases therefore both use a DECLARED
metric (``tool_call_accuracy``) so the breach is actually flagged.
"""

from __future__ import annotations

from typing import Any

from agent_sre.accuracy_declaration import AccuracyDeclaration


def run_accuracy_demo() -> dict[str, Any]:
    """Validate a declared metric at a compliant and a sub-threshold value.

    PASS: ``tool_call_accuracy`` = 0.99 meets the declared minimum (0.95) and is
    flagged COMPLIANT. INTERCEPT: 0.10 is below the minimum and flagged
    NON-COMPLIANT (the Art. 15(1) reference is emitted to the logger; the
    returned message states the threshold breach). Also exercises the
    undeclared-metric fail-open path to document that an unknown metric passes
    silently.
    """
    decl = AccuracyDeclaration.for_high_risk("answer-svc")

    declared_metrics = [t.metric_name for t in decl.declared_thresholds]

    pass_ok, pass_msg = decl.validate_against_sli("tool_call_accuracy", 0.99)
    breach_ok, breach_msg = decl.validate_against_sli("tool_call_accuracy", 0.10)
    undeclared_ok, undeclared_msg = decl.validate_against_sli(
        "undeclared_metric", 0.01
    )

    return {
        "system_name": "answer-svc",
        "risk_tier": decl.risk_tier.value,
        "declared_metrics": declared_metrics,
        "compliance_section": decl.to_compliance_section(),
        "pass": {
            "metric": "tool_call_accuracy",
            "value": 0.99,
            "compliant": pass_ok,
            "message": pass_msg,
        },
        "intercept": {
            "metric": "tool_call_accuracy",
            "value": 0.10,
            "compliant": breach_ok,
            "message": breach_msg,
        },
        "undeclared_fail_open": {
            "metric": "undeclared_metric",
            "value": 0.01,
            "compliant": undeclared_ok,
            "message": undeclared_msg,
        },
    }
