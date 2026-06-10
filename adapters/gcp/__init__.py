"""
adapters.gcp — GCP cloud bindings (SKELETON — WS6).

The contract is locked: ``PROVIDER`` implements ``core.interfaces.CloudProvider``
so ``CLOUD_PROVIDER=gcp`` resolves cleanly, but every accessor raises
``NotImplementedError`` until WS6 fills them in:

  identity  → per-agent Service Account + Workload Identity Federation
  secrets   → Secret Manager + Application Default Credentials (google-auth)
  tracing   → OTel → Cloud Trace
  audit     → hash-chain ledger on BigQuery / Spanner
  gateway   → Apigee → Vertex AI (or direct Vertex AI + ADC token)
  egress    → GCP endpoint allow-list (Vertex AI, Secret Manager, …)

See docs/REFACTOR_AND_GAPS_PLAN.md WS6.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

_WS = "WS6 — adapters/gcp not yet implemented"


def _todo(what: str):
    raise NotImplementedError(f"{_WS}: {what}")


class GcpProvider:
    name = "gcp"

    def secret_provider(self, **kwargs: Any):
        _todo("SecretProvider (Secret Manager + ADC)")

    def identity_provider(self):
        _todo("IdentityProvider (Service Account + Workload Identity Federation)")

    def trace_exporter_factory(self):
        _todo("TraceExporterFactory (Cloud Trace)")

    def llm_gateway(self):
        _todo("LLMGateway (Apigee → Vertex AI)")

    def runtime_adapter(self):
        # GCP uses its own framework adapter (Google ADK), not MAF.
        return None

    async def audit_backend(self, run_id: str):
        _todo("AuditBackend (BigQuery / Spanner hash-chain ledger)")

    def egress_config_path(self) -> Optional[Path]:
        path = Path(__file__).parent / "egress.yaml"
        return path if path.exists() else None


PROVIDER = GcpProvider()
