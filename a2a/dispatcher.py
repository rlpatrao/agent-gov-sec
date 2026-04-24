"""
a2a.dispatcher — single entrypoint for agent-to-agent calls.

Contract:
  1. Sender's audit logger records an `a2a_dispatch` event carrying the
     full request envelope.
  2. A child OTel span is opened for the recipient so the Application
     Insights trace tree shows one call under another.
  3. The recipient's `handler(request)` coroutine runs inside that span.
     The handler is expected to run the recipient's own MAF agent with its
     own middleware stack — so policy enforcement, anomaly detection, and
     audit logging all fire on the callee's identity independently.
  4. Sender's audit logger records an `a2a_reply` event with the response
     envelope and latency.
  5. Any exception raised in the handler is converted to an
     `A2AResponse(status=ERROR)` — callers never see raw tracebacks cross
     the A2A boundary. Exceptions are re-surfaced after logging so
     upstream circuit-breakers can react.

The dispatcher intentionally knows nothing about tree-sitter or scanner
specifics. It is a thin envelope-mover that owns the audit+trace contract.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False


A2AHandler = Callable[[A2ARequest], Awaitable[A2AResponse]]


async def a2a_call(
    request: A2ARequest,
    handler: A2AHandler,
    sender_audit: GovernanceAuditLogger,
    *,
    allowed_recipients: list[str] | None = None,
) -> A2AResponse:
    """Send an A2A request through the governance + trace pipeline.

    Args:
      request:           the envelope to send; `validate()` is called before
                         dispatch — missing provenance raises ValueError.
      handler:           coroutine that executes the recipient agent. Must
                         return an A2AResponse.
      sender_audit:      the *sender's* GovernanceAuditLogger. Dispatch and
                         reply events are logged here so every A2A hop
                         appears in the sender's hash-chained audit record.
      allowed_recipients: optional allow-list of recipient agent types (e.g.
                         ["ASTAnalyzer"]). If set and the recipient's type
                         (first "-"-delimited segment) isn't on the list,
                         the call is short-circuited with status=DENIED.
                         This is a belt-and-braces check on top of the YAML
                         policy pack for callers that want compile-time
                         certainty about who they may talk to.

    Returns:
      An A2AResponse. Status semantics are spelled out in A2AStatus.
    """
    request.validate()

    recipient_type = request.recipient.split("-", 1)[0]
    if allowed_recipients and recipient_type not in allowed_recipients:
        denied = A2AResponse.error(
            request=request,
            error=A2AError(
                code="recipient_not_allowed",
                message=(
                    f"Sender {request.sender} may not dispatch to "
                    f"{request.recipient}. Allowed recipients: {allowed_recipients}."
                ),
                details={"allowed_recipients": allowed_recipients},
            ),
            status=A2AStatus.DENIED,
        )
        _log_dispatch(sender_audit, request, outcome="deny",
                      reason=denied.payload.get("message", ""))
        _log_reply(sender_audit, denied, latency_ms=0.0, outcome="deny")
        return denied

    _log_dispatch(sender_audit, request, outcome="allow")

    t0 = time.monotonic()
    exc_to_reraise: Exception | None = None
    response: A2AResponse

    tracer = trace.get_tracer("galaxy.a2a") if _OTEL_AVAILABLE else None
    span_cm = (
        tracer.start_as_current_span(
            name=f"a2a.dispatch.{recipient_type}",
            attributes={
                "a2a.conversation_id": request.conversation_id,
                "a2a.message_id":      request.message_id,
                "a2a.sender":          request.sender,
                "a2a.recipient":       request.recipient,
                "a2a.intent":          request.intent,
                "a2a.payload_schema":  request.payload_schema,
                "galaxy.run_id":       request.run_id,
                "galaxy.module_id":    request.module_id,
            },
        )
        if tracer
        else _null_cm()
    )

    with span_cm as span:
        try:
            response = await handler(request)
        except Exception as e:
            exc_to_reraise = e
            response = A2AResponse.error(
                request=request,
                error=A2AError(
                    code="handler_raised",
                    message=str(e),
                    details={"exception_type": type(e).__name__},
                ),
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )
            if span is not None and hasattr(span, "record_exception"):
                span.record_exception(e)

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        response.latency_ms = response.latency_ms or elapsed_ms

        if span is not None and hasattr(span, "set_attribute"):
            span.set_attribute("a2a.status", response.status.value)
            span.set_attribute("a2a.latency_ms", response.latency_ms)

    outcome = "allow" if response.is_ok else "block" if response.status == A2AStatus.ERROR else "deny"
    _log_reply(sender_audit, response, latency_ms=response.latency_ms, outcome=outcome)

    if exc_to_reraise is not None:
        raise exc_to_reraise

    return response


# ── Audit helpers ─────────────────────────────────────────────────────────────

def _log_dispatch(
    audit: GovernanceAuditLogger,
    request: A2ARequest,
    *,
    outcome: str,
    reason: str = "",
) -> None:
    audit.log(AuditEntry(
        event_type="a2a_dispatch",
        agent_id=request.sender,
        action=f"a2a:{request.intent}",
        decision=outcome,
        reason=reason or f"Dispatching {request.intent} to {request.recipient}",
        latency_ms=0.0,
        metadata={
            "run_id":          request.run_id,
            "module_id":       request.module_id,
            "conversation_id": request.conversation_id,
            "message_id":      request.message_id,
            "recipient":       request.recipient,
            "payload_schema":  request.payload_schema,
            "input_summary":   f"{request.intent} → {request.recipient}",
        },
    ))


def _log_reply(
    audit: GovernanceAuditLogger,
    response: A2AResponse,
    *,
    latency_ms: float,
    outcome: str,
) -> None:
    audit.log(AuditEntry(
        event_type="a2a_reply",
        agent_id=response.recipient,   # the original sender receives the reply
        action=f"a2a_reply:{response.status.value}",
        decision=outcome,
        reason=_summarize_reply(response),
        latency_ms=latency_ms,
        metadata={
            "run_id":          response.run_id,
            "module_id":       response.module_id,
            "conversation_id": response.conversation_id,
            "message_id":      response.message_id,
            "in_reply_to":     response.in_reply_to,
            "responder":       response.sender,
            "status":          response.status.value,
            "payload_schema":  response.payload_schema,
            "input_summary":   f"reply from {response.sender}: {response.status.value}",
        },
    ))


def _summarize_reply(response: A2AResponse) -> str:
    if response.is_ok:
        return f"{response.sender} replied ok ({response.payload_schema})"
    err = response.payload or {}
    return f"{response.sender} replied {response.status.value}: {err.get('message', '')}"[:200]


class _null_cm:
    """No-op context manager used when OTel is unavailable."""
    def __enter__(self):
        return None
    def __exit__(self, exc_type, exc, tb):
        return False
