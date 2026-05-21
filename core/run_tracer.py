"""
run_tracer.py — lightweight OTel root span for Galaxy pipeline runs.

configure_tracing() wires the exporter once at process startup.
pipeline_span() creates a single root span that all five agent invocations
(emitted by MAF's AgentTelemetryLayer) become children of, giving one
operation_Id per run in Application Insights.

NHI attribution per-agent is preserved in governance audit events via
OtelAuditBackend (governance.agent_id on span events), which uses
trace.get_current_span() and attaches to MAF's child spans directly.
"""

import os
import logging
from contextlib import contextmanager
from typing import Generator, Optional

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    logger.warning("opentelemetry not installed — tracing disabled")


# ── Global setup ──────────────────────────────────────────────────────────────

def configure_tracing(service_name: str = None) -> None:
    """
    Call once at process startup. Routing:

      1. APPLICATIONINSIGHTS_CONNECTION_STRING set → direct-export to App
         Insights via azure-monitor-opentelemetry-exporter (preferred; no
         collector needed, works from laptop or ACA).
      2. OTEL_EXPORTER_OTLP_ENDPOINT set → generic OTLP gRPC export (for
         a locally-running otel-collector, or AKS with a collector sidecar).
      3. Neither set → no-exporter tracing (safe default for unit tests).

    When Microsoft Agent Framework is importable we route through MAF's
    `configure_otel_providers` so the ChatTelemetryLayer / AgentTelemetryLayer
    fire and emit the standard `gen_ai.*` semantic-convention spans that the
    Azure portal "Agents (preview)" dashboard queries for. Falls back to a
    minimal TracerProvider if MAF isn't installed.
    """
    if not _OTEL_AVAILABLE:
        return

    name = service_name or os.environ.get("OTEL_SERVICE_NAME", "galaxy-platform")
    os.environ.setdefault("OTEL_SERVICE_NAME", name)
    ai_conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    exporters = []
    exporter_kind = "none"
    if ai_conn:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
            exporters.append(AzureMonitorTraceExporter(connection_string=ai_conn))
            exporter_kind = "azure_monitor"
        except ImportError:
            logger.warning("tracing.azure_monitor_exporter_missing")
    elif otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporters.append(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
            exporter_kind = "otlp_grpc"
        except ImportError:
            logger.warning("tracing.otlp_exporter_missing")

    # Preferred path: let MAF own the providers + instrumentation layers so
    # gen_ai.* semantic conventions land correctly.
    try:
        from agent_framework.observability import configure_otel_providers
        configure_otel_providers(
            exporters=exporters or None,
            enable_sensitive_data=False,
        )
        logger.info(
            "tracing.configured",
            extra={"service": name, "exporter": exporter_kind, "via": "agent_framework"},
        )
        return
    except ImportError:
        pass

    # Fallback when MAF isn't installed.
    resource = Resource.create({"service.name": name, "service.namespace": "galaxy"})
    provider = TracerProvider(resource=resource)
    for exp in exporters:
        provider.add_span_processor(BatchSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    logger.info(
        "tracing.configured",
        extra={"service": name, "exporter": exporter_kind, "via": "fallback"},
    )


# ── Per-run root span ─────────────────────────────────────────────────────────

@contextmanager
def pipeline_span(run_id: str, module: str) -> Generator:
    """Root span for one pipeline run.

    All MAF AgentTelemetryLayer spans created during the run become children
    of this span, giving one operation_Id that covers the full
    Analyzer → Coder → Tester → Reviewer → SecurityReviewer chain in
    Application Insights.

    NHI IDs are not stamped here because each agent carries a different NHI.
    Per-agent NHI attribution is available via governance.agent_id on the
    span events written by OtelAuditBackend for every governance decision.
    """
    if not _OTEL_AVAILABLE:
        yield None
        return
    tracer = trace.get_tracer("galaxy.pipeline")
    with tracer.start_as_current_span(
        "pipeline.run",
        attributes={
            "galaxy.run_id": run_id,
            "galaxy.module":  module,
        },
    ) as span:
        yield span
