"""
adapters.gcp.tracing — GCP TraceExporterFactory (WS6).

Builds an OTLP span exporter pointed at an **OpenTelemetry Collector** that
forwards to **Cloud Trace** (the googlecloud exporter / collector). We export
OTLP rather than talking to Cloud Trace directly so the agnostic OTel SDK setup
in ``core/run_tracer.py`` stays cloud-neutral — only the collector endpoint is
GCP-specific.

Returns ``None`` when no collector endpoint is configured (so the core falls
back to a no-op/OTLP-default provider) or when the OTLP exporter package is
absent. The exporter import is lazy so importing this module needs no extra deps.

Env:
  OTEL_EXPORTER_OTLP_ENDPOINT  — the collector forwarding to Cloud Trace
                                 (e.g. http://localhost:4317)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class GcpTraceExporterFactory:
    """TraceExporterFactory impl backed by OTLP → Collector → Cloud Trace."""

    def create_span_exporter(self) -> Optional[Any]:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            return None
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError:
            logger.warning("tracing.otlp_exporter_missing — pip install opentelemetry-exporter-otlp")
            return None
        return OTLPSpanExporter(endpoint=endpoint)
