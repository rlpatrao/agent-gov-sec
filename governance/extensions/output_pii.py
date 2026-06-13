"""
governance.extensions.output_pii — output PII detection and masking guard.

Wraps ``agent_os.credential_redactor.CredentialRedactor`` to handle PII in model
output on the ``after_model`` seam. The existing pipeline already uses the
redactor for credential leaks on the input side; this guard covers the output
PII gap.

PACKAGING QUIRK (verified at runtime): PII detection here is detection-only.
``CredentialRedactor.redact()`` iterates only ``cls.PATTERNS`` (credentials),
never ``cls.PII_PATTERNS`` — so ``redact('john@acme.com SSN 123-45-6789')``
returns the string unchanged even though ``contains_pii()`` is True, and there
is no built-in ``redact_pii``/``mask_pii``. This guard therefore does its own
masking: it enumerates ``find_pii_matches`` and replaces each matched span with
``[REDACTED-<type>]``.

``redact_output`` masks and allows (audit posture): IPv4 and US-phone patterns
are broad and false-positive on benign numerics/log lines, so audit-and-mask is
safer than a hard deny for the default path. A ``strict`` mode blocks instead,
for callers that want fail-closed on any detected PII.

The module does not import the pipeline; it returns a verdict.
"""

from __future__ import annotations

from agent_os.credential_redactor import CredentialMatch, CredentialRedactor

from governance.extensions.decision import GuardDecision

BLOCK_CODE = "output_pii"


def _mask_label(pii_type: str) -> str:
    """Compact, stable token from a PII type name, e.g. 'US SSN' -> 'US-SSN'."""
    return "-".join(pii_type.upper().split())


class OutputPiiGuard:
    """Detect and mask PII in model output.

    Built once. ``redact_output`` masks detected PII in place and allows
    (carrying the masked text on ``output`` and the detected types on metadata);
    ``strict`` mode instead blocks with ``output_pii`` when any PII is found.
    The methods are pure and return a :class:`GuardDecision`.
    """

    def __init__(self, redactor: type[CredentialRedactor] = CredentialRedactor) -> None:
        # The redactor is a classmethod-only utility (no instance state); hold
        # the class so a test or caller can inject a configured subclass.
        self._redactor = redactor

    def _matches(self, text: str) -> list[CredentialMatch]:
        return self._redactor.find_pii_matches(text)

    def _mask(self, text: str, matches: list[CredentialMatch]) -> str:
        masked = text
        # Replace longest spans first so a shorter span is not consumed by an
        # overlapping longer one.
        for match in sorted(matches, key=lambda m: len(m.matched_text), reverse=True):
            masked = masked.replace(
                match.matched_text, f"[REDACTED-{_mask_label(match.name)}]"
            )
        return masked

    def redact_output(self, text: str, *, strict: bool = False) -> GuardDecision:
        """Detect PII in ``text`` and mask it (or block in ``strict`` mode).

        Default (audit) posture: always ``allowed=True``; ``output`` carries the
        text with every detected PII span replaced by ``[REDACTED-<type>]``, and
        ``metadata['pii_types']`` lists the distinct types found. ``strict=True``
        blocks with ``output_pii`` when any PII is detected, naming the types in
        the reason.
        """
        matches = self._matches(text)

        if not matches:
            return GuardDecision(
                allowed=True,
                reason="no PII detected in output",
                signals=["output_pii_clean"],
                metadata={"pii_types": []},
                output=text,
            )

        # Distinct types, order-preserving.
        pii_types: list[str] = []
        for m in matches:
            if m.name not in pii_types:
                pii_types.append(m.name)

        if strict:
            return GuardDecision.block(
                BLOCK_CODE,
                f"PII in output: {', '.join(pii_types)}",
                signals=["output_pii_blocked"],
                pii_types=pii_types,
            )

        masked = self._mask(text, matches)
        return GuardDecision(
            allowed=True,
            reason=f"masked PII in output: {', '.join(pii_types)}",
            signals=["output_pii_masked"],
            metadata={"pii_types": pii_types, "match_count": len(matches)},
            output=masked,
        )
