"""Tests for the ops-supplychain operational report functions.

Exercises the real agent_sre / agent_os symbols (no upstream mocking):
- SBOM: the declared DEPENDS_ON relationship survives SPDX serialisation.
- Signing: a pristine artifact verifies True; a tampered artifact verifies False.
- Certification: a passing tier ruling issues a certificate; a missing required
  evidence value withholds it.
- Adversarial: the built-in vectors yield a defense rate; dropping the controls
  proves the harness detects a real governance hole.
"""

from __future__ import annotations

from agent_sre.certification import CertificationTier

from governance.ops.adversarial_harness import (
    DefaultInterceptor,
    run_adversarial,
)
from governance.ops.certification_report import run_certification_demo
from governance.ops.sbom_report import run_sbom_demo
from governance.ops.signing_report import run_signing_demo


# --------------------------------------------------------------------------- #
# SBOM
# --------------------------------------------------------------------------- #
def test_sbom_records_dependency_relationship() -> None:
    report = run_sbom_demo()

    # The declared dependency appears as a package in the SPDX document.
    assert "anthropic" in report["package_names"]
    assert "demo-agent" in report["package_names"]

    # The DEPENDS_ON edge survived serialisation (not a fixed template).
    assert report["relationship_present"] is True

    # CycloneDX is also emitted as a dict.
    assert isinstance(report["cyclonedx"], dict)


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def test_signing_detects_tampering(tmp_path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"release payload v1.0.0")

    report = run_signing_demo(str(artifact))

    # PASS case: pristine artifact verifies.
    assert report["verified_clean"] is True
    # INTERCEPT case: tampered artifact fails verification.
    assert report["verified_tampered"] is False
    assert report["tamper_detected"] is True


# --------------------------------------------------------------------------- #
# Certification
# --------------------------------------------------------------------------- #
def test_certification_passes_with_full_evidence() -> None:
    evidence = {
        "sbom_signed": True,
        "slo_compliant": True,
        "eval_passed": True,
    }
    report = run_certification_demo(evidence, tier=CertificationTier.SILVER)

    assert report["passed"] is True
    assert report["certificate_id"]  # non-empty id issued


def test_certification_withholds_on_missing_evidence() -> None:
    # slo_compliant absent/False -> a required criterion fails.
    evidence = {
        "sbom_signed": True,
        "slo_compliant": False,
        "eval_passed": True,
    }
    report = run_certification_demo(evidence, tier=CertificationTier.SILVER)

    assert report["passed"] is False
    assert report["certificate_id"] == ""

    failed = [c for c in report["criteria_results"] if not c["passed"]]
    assert any(c["name"] == "slo_met" for c in failed)


# --------------------------------------------------------------------------- #
# Adversarial harness
# --------------------------------------------------------------------------- #
def test_adversarial_default_interceptor_defends() -> None:
    # PASS-the-test case: the default interceptor blocks the known-bad vectors.
    report = run_adversarial()

    assert report["total"] == 8
    assert report["failed"] == 0
    assert report["risk_score"] == 0.0
    assert report["defense_rate"] == 1.0

    by_name = {r["name"]: r for r in report["results"]}
    # shell_exec tool abuse is blocked.
    assert by_name["dangerous_shell"]["actual"] == "blocked"
    assert by_name["dangerous_shell"]["passed"] is True
    # prompt-injection override is blocked by pattern match.
    assert by_name["system_prompt_override"]["actual"] == "blocked"
    assert by_name["system_prompt_override"]["passed"] is True


def test_adversarial_detects_governance_hole() -> None:
    # GAP-proving case: drop shell_exec from the blocked tools and remove the
    # injection pattern. Those vectors now come back allowed -> the harness
    # reports failures, a non-zero risk score, and names the missing controls.
    weak = DefaultInterceptor(
        blocked_tools=[],  # shell_exec / file_access now reachable
        blocked_patterns=["nonexistent-pattern-that-matches-nothing"],
    )
    report = run_adversarial(interceptor=weak)

    assert report["failed"] > 0
    assert report["risk_score"] > 0.0
    assert report["defense_rate"] < 1.0

    by_name = {r["name"]: r for r in report["results"]}
    assert by_name["dangerous_shell"]["actual"] == "allowed"
    assert by_name["dangerous_shell"]["passed"] is False

    rec_text = " ".join(report["recommendations"]).lower()
    assert "shell_exec" in rec_text or "allowlist" in rec_text
