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
    Call once at process startup. Routing (cloud-agnostic):

      1. The selected cloud provider's TraceExporterFactory yields an exporter
         (Azure → Azure Monitor, AWS → X-Ray, GCP → Cloud Trace) when its
         connection config is present. Resolved via core.provider_factory.
      2. OTEL_EXPORTER_OTLP_ENDPOINT set → generic OTLP gRPC export (for a
         locally-running otel-collector, or a collector sidecar). Used when the
         provider yields no exporter.
      3. Neither → no-exporter tracing (safe default for unit tests/offline demo).

    The agent framework (via the provider's AgentRuntimeAdapter — MAF on Azure)
    may own provider setup so its ChatTelemetryLayer / AgentTelemetryLayer fire
    and emit the standard `gen_ai.*` semantic-convention spans. If no runtime
    adapter handles setup, we fall back to a minimal agnostic TracerProvider.

    This module imports no cloud SDK and no agent framework — both are reached
    only through the provider factory.
    """
    if not _OTEL_AVAILABLE:
        return

    name = service_name or os.environ.get("OTEL_SERVICE_NAME", "galaxy-platform")
    os.environ.setdefault("OTEL_SERVICE_NAME", name)
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    from core.provider_factory import get_provider

    exporters = []
    exporter_kind = "none"

    # 1. Cloud provider's span exporter (Azure Monitor / X-Ray / Cloud Trace).
    try:
        cloud_exporter = get_provider().trace_exporter_factory().create_span_exporter()
        if cloud_exporter is not None:
            exporters.append(cloud_exporter)
            exporter_kind = "cloud"
    except NotImplementedError:
        pass  # provider skeleton (aws/gcp) — fall through to OTLP/no-op
    except Exception as e:
        logger.warning("tracing.cloud_exporter_unavailable", extra={"error": str(e)})

    # 2. Agnostic OTLP fallback when no cloud exporter is configured.
    if not exporters and otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporters.append(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
            exporter_kind = "otlp_grpc"
        except ImportError:
            logger.warning("tracing.otlp_exporter_missing")

    # 3. Let the agent-framework runtime own provider setup if it can.
    try:
        runtime = get_provider().runtime_adapter()
        if runtime is not None and runtime.configure_observability(exporters or None):
            logger.info(
                "tracing.configured",
                extra={"service": name, "exporter": exporter_kind, "via": "runtime_adapter"},
            )
            return
    except Exception as e:
        logger.warning("tracing.runtime_adapter_unavailable", extra={"error": str(e)})

    # Agnostic fallback when no runtime adapter handled setup.
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
