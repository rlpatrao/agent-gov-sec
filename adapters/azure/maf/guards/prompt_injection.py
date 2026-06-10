"""
PromptInjectionGuardMiddleware — wraps `agent_os.prompt_injection.PromptInjectionDetector`
as a MAF AgentMiddleware.

Replaces the YAML regex rule in galaxy-core.yaml with a 7-vector taxonomy:
  - direct_override
  - delimiter_attack
  - encoding_attack
  - role_play
  - context_manipulation
  - canary_leak
  - multi_turn_escalation

Threat levels: NONE | LOW | MEDIUM | HIGH | CRITICAL.
We block on >= MEDIUM by default; LOW gets logged-as-audit but allowed through.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agent_framework._middleware import AgentMiddleware, MiddlewareTermination
from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger
from agent_os.prompt_injection import (
    DetectionConfig,
    DetectionResult,
    PromptInjectionDetector,
    ThreatLevel,
    load_prompt_injection_config,
)

logger = logging.getLogger(__name__)


_BLOCK_AT_OR_ABOVE: dict[str, int] = {
    "none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}


class PromptInjectionGuardMiddleware(AgentMiddleware):
    """Pre-dispatch guard. Calls `detector.detect()` on the last user message;
    raises MiddlewareTermination when threat >= block_threshold."""

    def __init__(
        self,
        agent_id: str,
        audit_log: Optional[GovernanceAuditLogger] = None,
        config_path: Optional[Path] = None,
        block_threshold: ThreatLevel = ThreatLevel.MEDIUM,
    ) -> None:
        if config_path and config_path.exists():
            cfg = load_prompt_injection_config(str(config_path))
            # Toolkit packaging bug: load_prompt_injection_config doesn't set
            # several attrs that _detect_impl reads. Backfill empty defaults
            # so detection actually runs (without these the detector
            # fails-closed on every call and returns critical-threat).
            for attr in ("allowlist", "blocklist", "custom_patterns"):
                if not hasattr(cfg, attr):
                    setattr(cfg, attr, [])
            if not hasattr(cfg, "sensitivity"):
                cfg.sensitivity = "balanced"
        else:
            cfg = None  # detector falls back to its sample rules
        self._detector = PromptInjectionDetector(cfg)
        self._agent_id = agent_id
        self._audit = audit_log
        self._block_at = _BLOCK_AT_OR_ABOVE[block_threshold.value]

    async def process(self, context: Any, call_next: Callable[[], Awaitable[None]]) -> None:
        last_message_text = _last_user_message(context)
        if not last_message_text:
            await call_next()
            return

        result: DetectionResult = self._detector.detect(last_message_text, source=self._agent_id)
        threat_idx = _BLOCK_AT_OR_ABOVE.get(result.threat_level.value, 0)
        block = result.is_injection and threat_idx >= self._block_at

        self._audit_decision(result, blocked=block)

        if block:
            logger.info(
                "prompt_injection.blocked",
                extra={
                    "agent_id": self._agent_id,
                    "type": result.injection_type.value if result.injection_type else "unknown",
                    "threat": result.threat_level.value,
                    "confidence": result.confidence,
                },
            )
            raise MiddlewareTermination(
                f"Prompt injection blocked ({result.injection_type.value if result.injection_type else 'unknown'},"
                f" threat={result.threat_level.value}, confidence={result.confidence:.2f}): {result.explanation}"
            )

        await call_next()

    def _audit_decision(self, r: DetectionResult, *, blocked: bool) -> None:
        if self._audit is None:
            return
        decision = "deny" if blocked else ("audit" if r.is_injection else "allow")
        self._audit.log(AuditEntry(
            event_type="prompt_injection_check",
            agent_id=self._agent_id,
            action="prompt_injection_detect",
            decision=decision,
            reason=r.explanation,
            metadata={
                "threat": r.threat_level.value,
                "type":   (r.injection_type.value if r.injection_type else "none"),
                "confidence":   round(r.confidence, 3),
                "matched_patterns": r.matched_patterns[:8],
            },
        ))


def _last_user_message(context: Any) -> str:
    """Best-effort extraction across MAF context shape drift."""
    msgs = getattr(context, "messages", None) or []
    if not msgs:
        return ""
    last = msgs[-1]
    return getattr(last, "text", None) or str(last)
