"""
adapters.aws.tracing — AWS TraceExporterFactory.

Builds an OTLP span exporter pointed at the **ADOT (AWS Distro for OpenTelemetry)
collector**, which forwards to X-Ray and/or CloudWatch. We export OTLP rather
than talking to X-Ray directly so the agnostic OTel SDK setup in
``core/run_tracer.py`` stays cloud-neutral — only the collector endpoint is
AWS-specific.

Returns ``None`` when no collector endpoint is configured (so the core falls
back to a no-op/OTLP-default provider) or when the OTLP exporter package is
absent. The exporter import is lazy so importing this module needs no extra deps.

Env:
  OTEL_EXPORTER_OTLP_ENDPOINT  — the ADOT collector (e.g. http://localhost:4317)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AwsTraceExporterFactory:
    """TraceExporterFactory impl backed by OTLP → ADOT collector → X-Ray/CloudWatch."""

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
