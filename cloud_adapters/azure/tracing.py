"""
cloud_adapters.azure.tracing — Azure TraceExporterFactory.

Builds the Azure Monitor OTel span exporter when
``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set (direct export, no collector —
works from a laptop or ACA). Returns ``None`` otherwise, letting the agnostic
core fall back to OTLP or a no-op provider.

The ``azure.monitor.*`` import is lazy so importing this module needs no Azure
SDK. AWS (X-Ray/ADOT) and GCP (Cloud Trace) ship sibling factories.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AzureTraceExporterFactory:
    """TraceExporterFactory impl backed by Azure Monitor."""

    def create_span_exporter(self) -> Optional[Any]:
        ai_conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
        if not ai_conn:
            return None
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        except ImportError:
            logger.warning("tracing.azure_monitor_exporter_missing")
            return None
        return AzureMonitorTraceExporter(connection_string=ai_conn)
