"""
cloud_adapters/aws/agentcore/runtime_adapter.py — the AWS AgentRuntimeAdapter.

Implements ``core.interfaces.AgentRuntimeAdapter`` for AWS. Two responsibilities:

* ``configure_observability`` — when the persona runs on **AgentCore Runtime**,
  the managed runtime emits CloudWatch/X-Ray traces for each session; this hook
  additionally wires an OTLP→ADOT span exporter (via ``AwsTraceExporterFactory``)
  onto the active tracer provider when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, so
  the platform's own ``pipeline.run`` spans land alongside the managed traces.
  Returns ``True`` when it installed an exporter, else ``False`` so the agnostic
  fallback in ``core/run_tracer.py`` runs unchanged.

Returning a real adapter (rather than ``None``) is what marks AWS as having a
hosted-runtime binding — the personas are deployed as AgentCore Runtimes by
``scripts/deploy_agentcore.py`` and serve the contract in ``runtime_agent.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AwsRuntimeAdapter:
    """``AgentRuntimeAdapter`` for AWS / AgentCore Runtime."""

    def configure_observability(self, exporters: Optional[list[Any]]) -> bool:
        from cloud_adapters.aws.tracing import AwsTraceExporterFactory

        span_exporter = AwsTraceExporterFactory().create_span_exporter()
        if span_exporter is None:
            return False  # no ADOT endpoint / exporter pkg — let the agnostic path run
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = trace.get_tracer_provider()
            if not isinstance(provider, TracerProvider):
                provider = TracerProvider()
                trace.set_tracer_provider(provider)
            provider.add_span_processor(BatchSpanProcessor(span_exporter))
            if exporters is not None:
                exporters.append(span_exporter)
            logger.info("aws_runtime_adapter.observability_configured")
            return True
        except ImportError:
            logger.warning("aws_runtime_adapter.otel_sdk_missing — falling back to agnostic tracing")
            return False
