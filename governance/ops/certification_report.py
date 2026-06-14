"""Certification release-gate report for the supply-chain demo section.

Operational release gate (flag: GALAXY_OPS_CERTIFICATION). Aggregates
evidence (SBOM signed, SLO compliance, evaluation pass) into a tier ruling.
A required criterion failing forces the result to fail and withholds the
certificate id. Not a per-call gate.

Each ``check_fn`` reads its evidence key with a ``False`` default so absent
evidence is treated as a failure (fail-closed), per the discovery note.
"""

from __future__ import annotations

from typing import Any

from agent_sre.certification import (
    CertificationEvaluator,
    CertificationTier,
    Criterion,
)


def _build_criteria() -> list[Criterion]:
    """Construct the fail-closed criteria used by the release gate."""
    return [
        Criterion(
            name="has_sbom",
            description="Signed SBOM present",
            tier=CertificationTier.BRONZE,
            check_fn=lambda e: bool(e.get("sbom_signed", False)),
            evidence_key="sbom_signed",
            required=True,
        ),
        Criterion(
            name="slo_met",
            description="SLO compliant",
            tier=CertificationTier.SILVER,
            check_fn=lambda e: bool(e.get("slo_compliant", False)),
            evidence_key="slo_compliant",
            required=True,
        ),
        Criterion(
            name="eval_passed",
            description="Adversarial evaluation passed",
            tier=CertificationTier.SILVER,
            check_fn=lambda e: bool(e.get("eval_passed", False)),
            evidence_key="eval_passed",
            required=True,
        ),
    ]


def _result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "tier": result.tier.value,
        "passed": result.passed,
        "agent_id": result.agent_id,
        "certificate_id": result.certificate_id,
        "criteria_results": [
            {
                "name": cr.name,
                "passed": cr.passed,
                "required": cr.required,
                "message": cr.message,
            }
            for cr in result.criteria_results
        ],
    }


def run_certification_demo(
    evidence: dict[str, Any],
    tier: CertificationTier = CertificationTier.SILVER,
    agent_id: str = "demo-agent",
) -> dict[str, Any]:
    """Evaluate ``evidence`` against ``tier`` and return a ruling report.

    A passing evidence dict yields ``passed=True`` with a non-empty
    ``certificate_id``; a dict missing a required value yields ``passed=False``
    with an empty ``certificate_id`` and the failing criterion named.
    """
    evaluator = CertificationEvaluator(criteria=_build_criteria())
    result = evaluator.evaluate(tier, evidence, agent_id=agent_id)
    return _result_to_dict(result)
