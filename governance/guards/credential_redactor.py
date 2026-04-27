"""
CredentialRedactorGuardMiddleware — wraps `agent_os.credential_redactor.CredentialRedactor`
as a MAF AgentMiddleware.

Detects API keys, tokens, AWS access keys, GitHub tokens, etc. in the user
message before dispatch. Two modes:

  - "redact" (default): mutate the message to replace credentials with [REDACTED];
    the call still proceeds with the cleaned text.
  - "deny": reject the call entirely if any credentials are found.

Always emits an audit entry naming the credential *types* found (never the
secrets themselves).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Literal, Optional

from agent_framework._middleware import AgentMiddleware, MiddlewareTermination
from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger
from agent_os.credential_redactor import CredentialRedactor

logger = logging.getLogger(__name__)


class CredentialRedactorGuardMiddleware(AgentMiddleware):
    """Pre-dispatch credential-strip / credential-deny guard."""

    def __init__(
        self,
        agent_id: str,
        audit_log: Optional[GovernanceAuditLogger] = None,
        mode: Literal["redact", "deny"] = "redact",
    ) -> None:
        self._redactor = CredentialRedactor()
        self._agent_id = agent_id
        self._audit = audit_log
        self._mode = mode

    async def process(self, context: Any, call_next: Callable[[], Awaitable[None]]) -> None:
        msgs = getattr(context, "messages", None) or []
        if not msgs:
            await call_next()
            return

        last = msgs[-1]
        original = getattr(last, "text", None) or ""
        if not original or not self._redactor.contains_credentials(original):
            await call_next()
            return

        matches = self._redactor.find_matches(original)
        types = sorted({m.name for m in matches})

        if self._mode == "deny":
            self._audit_decision(types, blocked=True, sample_count=len(matches))
            logger.info("credential_guard.deny", extra={"agent_id": self._agent_id, "types": types})
            raise MiddlewareTermination(
                f"Credential leak detected and blocked: {', '.join(types)}. "
                "Strip secrets from the prompt before retrying."
            )

        # redact mode: mutate the message in place
        cleaned = self._redactor.redact(original)
        if hasattr(last, "text"):
            try:
                last.text = cleaned  # type: ignore[attr-defined]
            except Exception:
                # Some message types are immutable; fall back to mutating contents
                if hasattr(last, "contents") and last.contents:
                    for c in last.contents:
                        if hasattr(c, "text"):
                            try:
                                c.text = cleaned
                                break
                            except Exception:
                                continue

        self._audit_decision(types, blocked=False, sample_count=len(matches))
        logger.info(
            "credential_guard.redacted",
            extra={"agent_id": self._agent_id, "types": types, "count": len(matches)},
        )
        await call_next()

    def _audit_decision(self, types: list[str], *, blocked: bool, sample_count: int) -> None:
        if self._audit is None:
            return
        self._audit.log(AuditEntry(
            event_type="credential_check",
            agent_id=self._agent_id,
            action="credential_scan",
            decision="deny" if blocked else "audit",
            reason=f"Detected {sample_count} credential match(es): {', '.join(types)}",
            metadata={"credential_types": types, "match_count": sample_count, "mode": self._mode},
        ))
