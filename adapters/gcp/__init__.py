"""
adapters.gcp — GCP cloud bindings (WS6).

``PROVIDER`` implements ``core.interfaces.CloudProvider`` so ``CLOUD_PROVIDER=gcp``
resolves the GCP bindings:

  identity  → per-agent Service Account + Workload Identity Federation   (identity.GcpIdentityProvider)
  secrets   → Secret Manager + Application Default Credentials           (secrets.SecretManagerProvider)
  tracing   → OTel → Collector → Cloud Trace                            (tracing.GcpTraceExporterFactory)
  gateway   → Apigee → Vertex AI (direct-Vertex/ADC fallback)            (gateway.GcpLLMGateway)
  audit     → SHA-256 hash-chain ledger on BigQuery (stdout fallback)    (audit.BigQueryHashChainBackend)
  egress    → GCP endpoint allow-list                                    (egress.yaml)

Every accessor lazy-imports its implementation, and each implementation
lazy-imports its Google SDK, so importing this package (or the provider factory)
needs no Google libraries. The framework axis (``runtime_adapter``) is
intentionally ``None`` — GCP wires LangGraph / Google ADK, not MAF. The live
Vertex/Gemini chat model is built by
``adapters/langgraph/runtime.build_gemini_model``.

See docs/REFACTOR_AND_GAPS_PLAN.md WS6.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

_EGRESS_YAML = Path(__file__).parent / "egress.yaml"


class GcpProvider:
    """CloudProvider impl: GCP cloud bindings."""

    name = "gcp"

    def secret_provider(self, **kwargs: Any):
        from adapters.gcp.secrets import SecretManagerProvider
        return SecretManagerProvider(**kwargs)

    def identity_provider(self):
        from adapters.gcp.identity import GcpIdentityProvider
        return GcpIdentityProvider()

    def trace_exporter_factory(self):
        from adapters.gcp.tracing import GcpTraceExporterFactory
        return GcpTraceExporterFactory()

    def llm_gateway(self):
        from adapters.gcp.gateway import GcpLLMGateway
        return GcpLLMGateway()

    def runtime_adapter(self):
        # GCP uses its own framework adapter (LangGraph / Google ADK), not MAF.
        return None

    async def audit_backend(self, run_id: str):
        from adapters.gcp.audit import BigQueryHashChainBackend
        return await BigQueryHashChainBackend.create(run_id=run_id)

    def egress_config_path(self) -> Optional[Path]:
        return _EGRESS_YAML if _EGRESS_YAML.exists() else None


PROVIDER = GcpProvider()
