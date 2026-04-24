"""
OtelAuditBackend — emits every governance AuditEntry as an OpenTelemetry
span event on the current span. App Insights picks these up through the
OTLP exporter configured in run_tracer.configure_tracing().

Kept intentionally small: the AuditEntry dataclass already has all the
fields we need; this class is a shim that copies them onto the current
span as an event named 'governance.<event_type>'.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_os.audit_logger import AuditEntry, AuditBackend

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False


class OtelAuditBackend(AuditBackend):
    """AuditBackend that writes to the current OTel span."""

    def write(self, entry: AuditEntry) -> None:
        if not _OTEL_AVAILABLE:
            return

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            # No active span — fall back to structured log so the event
            # is still visible in container logs ingested by App Insights.
            logger.info("otel_audit.no_active_span", extra={"event": entry.to_dict()})
            return

        attrs: dict[str, Any] = {
            "governance.agent_id":     entry.agent_id or "",
            "governance.event_type":   entry.event_type or "",
            "governance.action":       entry.action or "",
            "governance.decision":     entry.decision or "",
            "governance.reason":       (entry.reason or "")[:200],
            "governance.latency_ms":   float(entry.latency_ms or 0.0),
        }
        # Flatten metadata onto the span event; skip anything non-primitive.
        for k, v in (entry.metadata or {}).items():
            if isinstance(v, (str, int, float, bool)):
                attrs[f"governance.metadata.{k}"] = v

        span.add_event(
            name=f"governance.{entry.event_type or 'decision'}",
            attributes=attrs,
        )

        # Mirror deny/block decisions on the span status so they're visible
        # as errors in App Insights without requiring a custom query.
        if entry.decision in ("deny", "block"):
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, entry.reason or entry.decision))

    def flush(self) -> None:
        # OTel's own BatchSpanProcessor handles flushing; nothing to do here.
        pass
