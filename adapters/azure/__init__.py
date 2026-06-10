"""
adapters.azure — the Azure + Microsoft Agent Framework (MAF) binding.

This package holds *everything Microsoft-specific*: the Azure cloud bindings
(identity, secrets, tracing exporter, audit persistence, LLM gateway, egress
allow-list, infra) and, under ``maf/``, the MAF framework glue (guard
middlewares, the middleware assembly, the runtime/observability wiring).

``PROVIDER`` is the module-level ``CloudProvider`` that ``core.provider_factory``
resolves for ``CLOUD_PROVIDER=azure`` (the default). Every accessor lazy-imports
its implementation so importing this package needs no Azure SDK or MAF install.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

_EGRESS_YAML = Path(__file__).parent / "egress.yaml"


class AzureProvider:
    """CloudProvider impl: Azure cloud bindings + MAF runtime."""

    name = "azure"

    def secret_provider(self, **kwargs: Any):
        from adapters.azure.secrets import TokenProvider
        return TokenProvider(**kwargs)

    def identity_provider(self):
        from adapters.azure.identity import AzureIdentityProvider
        return AzureIdentityProvider()

    def trace_exporter_factory(self):
        from adapters.azure.tracing import AzureTraceExporterFactory
        return AzureTraceExporterFactory()

    def llm_gateway(self):
        from adapters.azure.gateway import AzureLLMGateway
        return AzureLLMGateway()

    def runtime_adapter(self):
        from adapters.azure.maf.runtime import MafRuntimeAdapter
        return MafRuntimeAdapter()

    async def audit_backend(self, run_id: str):
        from adapters.azure.audit import PostgresHashChainBackend
        return await PostgresHashChainBackend.create(run_id=run_id)

    def egress_config_path(self) -> Optional[Path]:
        return _EGRESS_YAML if _EGRESS_YAML.exists() else None


PROVIDER = AzureProvider()
