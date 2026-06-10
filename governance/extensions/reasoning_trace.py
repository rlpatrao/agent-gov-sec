"""
governance.extensions.reasoning_trace — Gap 4+: reasoning-content observability.

Today traces capture ``reasoning_tokens`` *counts* and per-step spans, but not
the reasoning *content*. This module captures the agent's Chain-of-Thought (CoT)
and Chain-of-Verification (CoVe) and logs them — as OTel span events and an audit
ledger record — so reasoning is attributable and tamper-evident alongside
actions. It is the **observability** complement to ``reasoning_guard`` (the
enforcement side), and reinforces our strongest pillar.

Non-negotiable: **redact before persist.** Reasoning text is high-risk for
leaking secrets/PII, so every CoT/CoVe string is routed through MSGK's
``CredentialRedactor`` (credentials + PII) before it touches any span, log, or
ledger. Raw reasoning never reaches a sink. Volume is bounded by sampling +
truncation (full content on deny/error, summarized on success).

Feature-flagged off by default (``GALAXY_GAP_REASONING_TRACE``).
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace
    _OTEL = True
except ImportError:  # pragma: no cover
    _OTEL = False

_DENY_DECISIONS = {"deny", "block", "error", "escalate"}


@dataclass(frozen=True)
class ReasoningTraceConfig:
    sample_rate: float = 1.0        # fraction of success-path traces to capture (deny/error always captured)
    max_chars_summary: int = 2000   # truncation on the success path
    max_chars_full: int = 16000     # hard cap even on deny/error
    redact: bool = True             # MANDATORY — left configurable only to fail the build if flipped


@dataclass
class ReasoningTraceRecord:
    agent_type: str
    nhi_id: str
    cot_hash: str = ""
    cove_hash: str = ""
    redaction_applied: bool = False
    sampled: bool = True
    decision: str = "allow"
    cot: str = ""                   # redacted + possibly truncated
    cove: str = ""

    def to_dict(self) -> dict:
        return {
            "agent_type": self.agent_type, "nhi_id": self.nhi_id,
            "cot_hash": self.cot_hash, "cove_hash": self.cove_hash,
            "redaction_applied": self.redaction_applied, "sampled": self.sampled,
            "decision": self.decision,
        }


class ReasoningTraceLogger:
    """Captures, redacts, and logs CoT/CoVe to OTel span events + the audit ledger."""

    def __init__(
        self,
        audit_backend: Optional[Any] = None,     # agent_os.audit_logger.AuditBackend
        config: Optional[ReasoningTraceConfig] = None,
        redactor: Optional[Any] = None,
    ) -> None:
        self._audit = audit_backend
        self._cfg = config or ReasoningTraceConfig()
        if redactor is not None:
            self._redactor = redactor
        else:
            try:
                from agent_os.credential_redactor import CredentialRedactor
                self._redactor = CredentialRedactor()
            except Exception:  # redaction is mandatory — refuse to run without it
                raise RuntimeError("reasoning_trace requires agent_os.credential_redactor for mandatory redaction")

    def capture(
        self,
        *,
        run_id: str,
        agent_type: str,
        nhi_id: str,
        cot: str,
        cove: Optional[str] = None,
        decision: str = "allow",
        module_id: str = "unknown",
    ) -> Optional[ReasoningTraceRecord]:
        """Redact, truncate, and emit one reasoning trace. Returns the record,
        or ``None`` if sampled out (deny/error paths are never sampled out)."""
        is_incident = decision in _DENY_DECISIONS
        if not is_incident and self._cfg.sample_rate < 1.0 and random.random() > self._cfg.sample_rate:
            return None

        cap = self._cfg.max_chars_full if is_incident else self._cfg.max_chars_summary
        cot_r, cot_changed = self._redact(cot)
        cove_r, cove_changed = self._redact(cove or "")
        cot_r, cove_r = cot_r[:cap], cove_r[:cap]
        redaction_applied = cot_changed or cove_changed

        record = ReasoningTraceRecord(
            agent_type=agent_type, nhi_id=nhi_id, decision=decision,
            cot=cot_r, cove=cove_r,
            cot_hash=_sha(cot_r), cove_hash=_sha(cove_r),
            redaction_applied=redaction_applied, sampled=True,
        )
        self._emit_span_events(record)
        self._emit_audit(record, run_id=run_id, module_id=module_id)
        return record

    # ── redaction (mandatory) ─────────────────────────────────────────────
    def _redact(self, text: str) -> tuple[str, bool]:
        if not text:
            return "", False
        original = text
        redacted = self._redactor.redact(text)        # credentials
        # PII pass (best-effort; never raise)
        try:
            if getattr(self._redactor, "contains_pii", None) and self._redactor.contains_pii(redacted):
                for m in self._redactor.find_pii_matches(redacted):
                    frag = getattr(m, "value", None) or getattr(m, "text", None) or getattr(m, "matched_text", None)
                    if frag:
                        redacted = redacted.replace(frag, "***PII***")
        except Exception as e:  # pragma: no cover
            logger.warning("reasoning_trace.pii_redaction_failed", extra={"error": str(e)})
        return redacted, (redacted != original)

    # ── sinks ──────────────────────────────────────────────────────────────
    def _emit_span_events(self, record: ReasoningTraceRecord) -> None:
        if not _OTEL:
            return
        span = _otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return
        base = {
            "governance.agent_id": record.nhi_id,
            "reasoning.decision": record.decision,
            "reasoning.redaction_applied": record.redaction_applied,
        }
        if record.cot:
            span.add_event("reasoning.cot", attributes={**base, "reasoning.cot_hash": record.cot_hash, "reasoning.cot": record.cot})
        if record.cove:
            span.add_event("reasoning.cove", attributes={**base, "reasoning.cove_hash": record.cove_hash, "reasoning.cove": record.cove})

    def _emit_audit(self, record: ReasoningTraceRecord, *, run_id: str, module_id: str) -> None:
        if self._audit is None:
            return
        try:
            from agent_os.audit_logger import AuditEntry
            self._audit.write(AuditEntry(
                event_type="reasoning_trace",
                agent_id=record.nhi_id,
                action="reasoning_trace",
                decision=record.decision,
                reason=(record.cot or "")[:200],
                metadata={
                    "run_id": run_id, "module_id": module_id, "nhi_id": record.nhi_id,
                    "agent_type": record.agent_type,
                    "cot_hash": record.cot_hash, "cove_hash": record.cove_hash,
                    "redaction_applied": record.redaction_applied,
                },
            ))
        except Exception as e:  # logging must never break the run
            logger.warning("reasoning_trace.audit_write_failed", extra={"error": str(e)})


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
