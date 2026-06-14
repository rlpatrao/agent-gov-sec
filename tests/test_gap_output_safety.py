"""
Tests for the output-safety subgroup (Tier D):
  - governance.extensions.content_quality.ContentQualityGuard
  - governance.extensions.output_pii.OutputPiiGuard

Each guard is exercised against the real agent_os symbol (no upstream mocking):
ContentQualityEvaluator applies the threshold/gate rules, and CredentialRedactor
supplies the PII matches. Both a PASS (allowed=True) and an INTERCEPT case are
asserted, drawn from the discovery demo_scenario. No wall-clock dependence.
"""

from __future__ import annotations

from agent_os.content_governance import ContentDimension, QualityGate
from governance.extensions.content_quality import (
    BLOCK_CODE as CONTENT_BLOCK_CODE,
)
from governance.extensions.content_quality import ContentQualityGuard
from governance.extensions.output_pii import BLOCK_CODE as PII_BLOCK_CODE
from governance.extensions.output_pii import OutputPiiGuard


# --------------------------------------------------------------------------- #
# ContentQualityGuard
# --------------------------------------------------------------------------- #


def test_content_quality_pass_grounded() -> None:
    """A grounded, cited answer clears the ACCURACY=0.8 FAIL gate."""
    guard = ContentQualityGuard()
    grounded = (
        "According to the 2023 annual report [1], revenue grew 12 percent year "
        "over year, source: https://example.com/report."
    )
    decision = guard.evaluate_output(grounded)

    assert decision.allowed is True
    assert decision.code == ""
    # Output-mutating guard forwards the (unmodified) text downstream.
    assert decision.output == grounded
    assert decision.metadata["overall_score"] >= 0.8


def test_content_quality_intercept_low_quality() -> None:
    """A hedged, unsupported answer trips the FAIL-gated ACCURACY rule."""
    guard = ContentQualityGuard()
    hallucinated = "I think it was probably around a billion, maybe more, not sure."
    decision = guard.evaluate_output(hallucinated)

    assert decision.allowed is False
    assert decision.code == CONTENT_BLOCK_CODE
    # The evaluator produced exactly one FAIL-gated failure (min-accuracy).
    assert len(decision.metadata["failures"]) == 1
    failure = decision.metadata["failures"][0]
    assert failure["rule"] == "min-accuracy"
    assert failure["dimension"] == ContentDimension.ACCURACY.value
    assert "threshold" in decision.reason


def test_content_quality_uses_real_evaluator_gate() -> None:
    """Sanity: the underlying ContentQualityEvaluator applies the FAIL gate."""
    guard = ContentQualityGuard()
    report = guard.report_for("I guess maybe, not sure, could be wrong.")
    assert report.passed is False
    assert any(f.gate_result == QualityGate.FAIL for f in report.failures)


# --------------------------------------------------------------------------- #
# OutputPiiGuard
# --------------------------------------------------------------------------- #


def test_output_pii_pass_clean() -> None:
    """Output with no PII passes through unchanged."""
    guard = OutputPiiGuard()
    decision = guard.redact_output("Your order has shipped.")

    assert decision.allowed is True
    assert decision.metadata["pii_types"] == []
    assert decision.output == "Your order has shipped."


def test_output_pii_masks_email_and_ssn() -> None:
    """Email and SSN spans are actually masked out of the output text."""
    guard = OutputPiiGuard()
    text = "Reach the customer at john@acme.com, SSN 123-45-6789"
    decision = guard.redact_output(text)

    assert decision.allowed is True
    # Real CredentialRedactor.find_pii_matches identified both types.
    assert "Email address" in decision.metadata["pii_types"]
    assert "US SSN" in decision.metadata["pii_types"]
    # The quirk: redact() would leave these intact; the guard masks them itself.
    assert "john@acme.com" not in decision.output
    assert "123-45-6789" not in decision.output
    assert "[REDACTED-EMAIL-ADDRESS]" in decision.output
    assert "[REDACTED-US-SSN]" in decision.output


def test_output_pii_strict_blocks() -> None:
    """Strict mode blocks with output_pii and names the detected types."""
    guard = OutputPiiGuard()
    text = "Reach the customer at john@acme.com, SSN 123-45-6789"
    decision = guard.redact_output(text, strict=True)

    assert decision.allowed is False
    assert decision.code == PII_BLOCK_CODE
    assert "Email address" in decision.reason
    assert "US SSN" in decision.reason
