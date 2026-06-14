"""
governance.extensions.content_quality — output content-quality gating guard.

Wraps ``agent_os.content_governance.ContentQualityEvaluator`` to gate model
output on the ``after_model`` seam. ``ContentQualityEvaluator.evaluate`` is
score-driven, not text-driven: it consumes a precomputed
``dict[ContentDimension, float]`` and applies the configured threshold/gate
rules; it does not analyze the response string itself. This wrapper supplies the
missing scoring step (``_score``) and feeds the scores into the evaluator.

The scorer here is a cheap heuristic — hedging markers, citation/grounding
presence, length, refusal markers. It is a stand-in for the production scoring
step; the intended production swap is an LLM judge (or a calibrated classifier)
that returns the same ``dict[ContentDimension, float]`` shape. The module is
otherwise unchanged: it applies thresholds/gates over whatever scores it is given.

Fail behavior: ``ContentQualityRule.gate`` defaults to ``WARN`` upstream, so a
fail-closed gate must explicitly set ``gate=QualityGate.FAIL``. This guard's
default rule set targets ACCURACY with ``gate=FAIL`` so a low-quality answer is
blocked rather than returned.

The module does not import the pipeline; ``evaluate_output`` returns a verdict.
"""

from __future__ import annotations

import re
from typing import Optional

from agent_os.content_governance import (
    ContentDimension,
    ContentQualityEvaluator,
    ContentQualityReport,
    ContentQualityRule,
    QualityGate,
)

from governance.extensions.decision import GuardDecision

BLOCK_CODE = "content_quality_failed"

# Heuristic lexicons. These are intentionally cheap signals; an LLM judge is the
# production replacement for ``_score``.
_HEDGING_MARKERS = (
    "i think",
    "i guess",
    "maybe",
    "probably",
    "might be",
    "not sure",
    "as far as i know",
    "i believe",
    "could be",
)
_REFUSAL_MARKERS = (
    "i cannot",
    "i can't",
    "i'm unable",
    "i am unable",
    "as an ai",
    "i do not have",
)
# Grounding signals: citations, source references, quoted spans, URLs.
_CITATION_RE = re.compile(
    r"(\[\d+\]|\(\d{4}\)|https?://|according to|per the|source:|cite|reference)",
    re.IGNORECASE,
)


class ContentQualityGuard:
    """Score model output and gate it on a quality threshold.

    Built once with a configured :class:`ContentQualityEvaluator`. The default
    construction loads a single fail-closed ACCURACY rule (threshold 0.8); pass
    a custom ``evaluator`` or ``rules`` to override. ``evaluate_output`` is a
    pure method returning a :class:`GuardDecision`.
    """

    def __init__(
        self,
        evaluator: Optional[ContentQualityEvaluator] = None,
        *,
        rules: Optional[list[ContentQualityRule]] = None,
        agent_id: str = "galaxy",
    ) -> None:
        if evaluator is None:
            evaluator = ContentQualityEvaluator()
            if rules is None:
                # Fail-closed by default: ACCURACY must clear 0.8 or the output
                # is blocked. Upstream rules default to gate=WARN, so FAIL is set
                # explicitly to get fail-closed behavior.
                rules = [
                    ContentQualityRule(
                        name="min-accuracy",
                        dimension=ContentDimension.ACCURACY,
                        threshold=0.8,
                        gate=QualityGate.FAIL,
                        description="Output must be grounded/accurate enough to return.",
                    )
                ]
            for rule in rules:
                evaluator.add_rule(rule)
        self._evaluator = evaluator
        self._agent_id = agent_id

    @property
    def evaluator(self) -> ContentQualityEvaluator:
        return self._evaluator

    def _score(self, text: str) -> dict[ContentDimension, float]:
        """Heuristic quality scorer over the response text.

        Returns a ``dict[ContentDimension, float]`` in [0, 1]. This is a cheap
        stand-in; the production swap is an LLM judge returning the same shape.
        Only dimensions this heuristic can estimate are populated — the
        evaluator treats absent dimensions as 0.0 but only fails them if a rule
        targets them with gate=FAIL.
        """
        body = (text or "").strip()
        lower = body.lower()
        words = body.split()
        n_words = len(words)

        # ACCURACY: penalize hedging, reward grounding/citations.
        hedges = sum(1 for m in _HEDGING_MARKERS if m in lower)
        grounded = bool(_CITATION_RE.search(body))
        accuracy = 0.6
        accuracy += 0.3 if grounded else 0.0
        accuracy -= 0.18 * hedges
        if n_words == 0:
            accuracy = 0.0
        accuracy = max(0.0, min(1.0, accuracy))

        # COMPLETENESS: very short answers are likely incomplete; refusals score low.
        refused = any(m in lower for m in _REFUSAL_MARKERS)
        if n_words == 0 or refused:
            completeness = 0.1
        elif n_words < 4:
            completeness = 0.4
        else:
            completeness = min(1.0, 0.5 + 0.02 * n_words)

        # STRUCTURE: sentence-terminated, non-empty text reads as structured.
        structure = 0.5
        if n_words == 0:
            structure = 0.0
        elif body.endswith((".", "!", "?")) or "\n" in body:
            structure = 0.9

        return {
            ContentDimension.ACCURACY: accuracy,
            ContentDimension.COMPLETENESS: completeness,
            ContentDimension.STRUCTURE: structure,
        }

    def report_for(self, text: str, *, content_id: str = "response") -> ContentQualityReport:
        """Score ``text`` and run it through the evaluator, returning the report."""
        scores = self._score(text)
        return self._evaluator.evaluate(self._agent_id, content_id, scores)

    def evaluate_output(self, text: str, *, content_id: str = "response") -> GuardDecision:
        """Gate model output on the configured quality rules.

        Blocks with ``content_quality_failed`` when ``report.passed`` is False
        (any FAIL-gated rule trips). On allow, carries the report's overall
        score and any warnings on the verdict metadata. The (unmodified) text is
        set on ``output`` so the pipeline can forward it downstream.
        """
        report = self.report_for(text, content_id=content_id)

        warnings = [
            {"rule": w.rule_name, "dimension": w.dimension.value, "score": w.score}
            for w in report.warnings
        ]

        if not report.passed:
            failures = [
                {"rule": f.rule_name, "dimension": f.dimension.value, "score": f.score, "detail": f.details}
                for f in report.failures
            ]
            reason = "; ".join(f.details for f in report.failures) or "content quality gate failed"
            return GuardDecision.block(
                BLOCK_CODE,
                reason,
                signals=["content_quality_failed"],
                overall_score=report.overall_score,
                failures=failures,
                warnings=warnings,
            )

        return GuardDecision(
            allowed=True,
            reason="content quality gate passed",
            signals=["content_quality_passed"],
            metadata={"overall_score": report.overall_score, "warnings": warnings},
            output=text,
        )
