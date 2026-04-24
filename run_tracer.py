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
    Call once at process startup. Three routing options, decided by env:

      1. APPLICATIONINSIGHTS_CONNECTION_STRING set → direct-export to App
         Insights via azure-monitor-opentelemetry-exporter (preferred; no
         collector needed, works from laptop or ACA).
      2. OTEL_EXPORTER_OTLP_ENDPOINT set → generic OTLP gRPC export (for
         a locally-running otel-collector, or AKS with a collector sidecar).
      3. Neither set → tracing configured with no exporter (spans visible
         in-process but not shipped anywhere; safe default for unit tests).
    """
    if not _OTEL_AVAILABLE:
        return

    name = service_name or os.environ.get("OTEL_SERVICE_NAME", "galaxy-platform")
    ai_conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({
        "service.name": name,
        "service.namespace": "galaxy",
    })
    provider = TracerProvider(resource=resource)

    exporter_kind = "none"
    if ai_conn:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
            provider.add_span_processor(
                BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=ai_conn))
            )
            exporter_kind = "azure_monitor"
        except ImportError:
            logger.warning(
                "tracing.azure_monitor_exporter_missing — install `azure-monitor-opentelemetry-exporter` "
                "or unset APPLICATIONINSIGHTS_CONNECTION_STRING"
            )
    elif otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
            )
            exporter_kind = "otlp_grpc"
        except ImportError:
            logger.warning("tracing.otlp_exporter_missing")

    trace.set_tracer_provider(provider)

    logger.info(
        "tracing.configured",
        extra={"service": name, "exporter": exporter_kind},
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

        with self._tracer.start_as_current_span(
            name=f"{agent_type}.run",
            attributes={
                # Galaxy semantic attributes
                "galaxy.run_id":      self.run_id,
                "galaxy.module_id":   self.module_id,
                "galaxy.agent_type":  agent_type,
                "galaxy.attempt":     attempt,
                "galaxy.nhi_id":      nhi_id,
                # gen_ai.* semantic conventions (OpenTelemetry standard)
                "gen_ai.system":      "anthropic",
                "gen_ai.operation.name": "chat",
            },
        ) as span:
            yield span

    def llm_span(
        self,
        agent_type: str,
        model: str,
        attempt: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        outcome: str = "success",
    ) -> None:
        """
        Records LLM call attributes on the current span.
        Call this after a successful foundry_client dispatch.
        """
        if not _OTEL_AVAILABLE:
            return

        current = trace.get_current_span()
        current.set_attribute("gen_ai.request.model", model)
        current.set_attribute("gen_ai.usage.prompt_tokens", prompt_tokens)
        current.set_attribute("gen_ai.usage.completion_tokens", completion_tokens)
        current.set_attribute("gen_ai.usage.total_tokens", prompt_tokens + completion_tokens)
        current.set_attribute("galaxy.attempt", attempt)
        current.set_attribute("galaxy.outcome", outcome)

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
