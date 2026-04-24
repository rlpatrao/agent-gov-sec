"""
run_tracer.py

OpenTelemetry trace context for the Galaxy platform.

Every Galaxy run has one root span. Every agent call is a child span.
Spans are propagated via W3C TraceContext headers on every APIM call so
Application Insights reconstructs the full execution tree.

One query by galaxy.run_id shows the complete call chain:
  run-001
  ├── Scanner.run          (attempt 1)
  │   └── llm_call         tokens=1240, latency=2.1s
  ├── Architect.run        (attempt 1)
  │   └── llm_call         tokens=3100, latency=4.8s
  └── Coder.run            (attempt 1, 2)
      ├── llm_call         tokens=4200, latency=6.2s  [attempt 1 — blocked]
      └── llm_call         tokens=3800, latency=5.9s  [attempt 2 — success]

Setup:
  Call RunTracer.configure() once at process startup (in main or FastAPI lifespan).
  Then instantiate RunTracer(run_id, module_id) per run.
"""

import os
import logging
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    from opentelemetry.context import attach, detach
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    logger.warning("opentelemetry not installed — tracing disabled")


# ── Global setup ─────────────────────────────────────────────────────────────

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

    # Build the exporter list we'll hand to whichever setup path we use.
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
            # Leave sensitive_data off by default — prompts/responses stay out
            # of telemetry. Can be overridden via ENABLE_OTEL_DIAGNOSTICS env.
            enable_sensitive_data=False,
        )
        logger.info(
            "tracing.configured",
            extra={"service": name, "exporter": exporter_kind, "via": "agent_framework"},
        )
        return
    except ImportError:
        pass

    # Fallback when MAF isn't installed — manual provider, no gen_ai layer.
    resource = Resource.create({"service.name": name, "service.namespace": "galaxy"})
    provider = TracerProvider(resource=resource)
    for exp in exporters:
        provider.add_span_processor(BatchSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    logger.info(
        "tracing.configured",
        extra={"service": name, "exporter": exporter_kind, "via": "fallback"},
    )


# ── Per-run tracer ────────────────────────────────────────────────────────────

class RunTracer:
    """
    One instance per Galaxy run.
    All agent spans are children of the root span for the run.
    """

    def __init__(self, run_id: str, module_id: str):
        self.run_id = run_id
        self.module_id = module_id
        self._tracer = trace.get_tracer("galaxy.platform") if _OTEL_AVAILABLE else None
        self._propagator = TraceContextTextMapPropagator() if _OTEL_AVAILABLE else None

    @contextmanager
    def agent_span(
        self,
        agent_type: str,
        attempt: int,
        nhi_id: str,
    ) -> Generator:
        """
        Context manager for one agent execution.

        Usage:
            with tracer.agent_span("Scanner", attempt=1, nhi_id=identity.client_id) as span:
                span.set_attribute("galaxy.files_found", 42)

        The span carries all standard Galaxy + gen_ai.* semantic attributes.
        Application Insights groups these automatically.
        """
        if not _OTEL_AVAILABLE or not self._tracer:
            yield _NoOpSpan()
            return

        # Galaxy-level agent span — intentionally carries NO gen_ai.* attributes.
        # The authoritative LLM span is emitted by MAF's ChatTelemetryLayer
        # (inside OpenAIChatClient); duplicating gen_ai.system here would
        # mislabel the transaction in App Insights (it previously read "anthropic"
        # from a stale hardcoded string even though the call was to Azure OpenAI).
        with self._tracer.start_as_current_span(
            name=f"{agent_type}.run",
            attributes={
                "galaxy.run_id":      self.run_id,
                "galaxy.module_id":   self.module_id,
                "galaxy.agent_type":  agent_type,
                "galaxy.attempt":     attempt,
                "galaxy.nhi_id":      nhi_id,
            },
        ) as span:
            yield span

    # Historical note: a `llm_span()` helper previously stamped gen_ai.*
    # attributes onto the current span from inside foundry_client.FoundryClient.
    # That custom dispatch path was deleted in Phase D — MAF's
    # ChatTelemetryLayer now emits the authoritative LLM span with correct
    # gen_ai.request.model + usage counts. No replacement needed here.

    def inject_headers(self) -> dict:
        """
        Returns W3C TraceContext headers to inject into outgoing APIM calls.
        APIM propagates traceparent + tracestate through to Application Insights.

        These go into extra_headers on every foundry_client dispatch alongside
        the x-agent-type, x-galaxy-run-id etc. headers.
        """
        if not _OTEL_AVAILABLE or not self._propagator:
            return {}

        headers: dict = {}
        self._propagator.inject(headers)
        return headers  # {"traceparent": "00-...", "tracestate": "..."}


class _NoOpSpan:
    """Returned when OTel is unavailable — prevents AttributeError in agent code."""
    def set_attribute(self, key: str, value) -> None:
        pass
    def record_exception(self, exc: Exception) -> None:
        pass
    def set_status(self, status) -> None:
        pass
