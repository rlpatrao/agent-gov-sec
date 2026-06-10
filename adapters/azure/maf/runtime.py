"""
adapters.azure.maf.runtime — the MAF AgentRuntimeAdapter.

Microsoft Agent Framework owns the OTel provider setup so its
ChatTelemetryLayer / AgentTelemetryLayer fire and emit the standard
``gen_ai.*`` semantic-convention spans the Azure "Agents (preview)" dashboard
queries. ``configure_observability`` routes the given exporters through MAF's
``configure_otel_providers`` and reports whether it handled setup.

This is the framework axis of the Azure bundle: when an AWS/GCP run wires a
different ``AgentRuntimeAdapter`` (LangGraph / Bedrock Agents / Google ADK),
this MAF wiring is simply not selected. The ``agent_framework`` import is lazy
so importing the package needs no MAF install.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MafRuntimeAdapter:
    """AgentRuntimeAdapter impl for the Microsoft Agent Framework."""

    def configure_observability(self, exporters: Optional[list[Any]]) -> bool:
        """Let MAF own the TracerProvider + instrumentation layers. Returns
        True if MAF handled setup, False if MAF isn't installed (caller then
        falls back to the agnostic TracerProvider)."""
        try:
            from agent_framework.observability import configure_otel_providers
        except ImportError:
            return False
        configure_otel_providers(
            exporters=exporters or None,
            enable_sensitive_data=False,
        )
        logger.info("tracing.configured", extra={"via": "agent_framework"})
        return True
