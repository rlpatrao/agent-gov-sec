"""
cloud_adapters.local — the cloud-neutral provider (no Azure/AWS/GCP SDK, no cloud creds).

For local/offline runs (and `CLOUD_PROVIDER=local`): identity + secrets come from
env, the LLM gateway refuses to egress (no key), the audit ledger is in-memory
hash-chained, and there's a minimal localhost-only egress allow-list. Nothing
here is branded to a cloud — the demo runs identically, just without cloud
adapter logs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from core.interfaces import EgressResolution
from core.secrets import EnvVarSecretProvider

_EGRESS_YAML = Path(__file__).parent / "egress.yaml"


class LocalIdentityProvider:
    """Resolves the agent's id from ``NHI_CLIENT_ID_<AGENT_TYPE>`` env; no cloud
    directory, no credential exchange (returns None — offline)."""

    def resolve_client_id(self, *, agent_type: str) -> Optional[str]:
        return os.environ.get(f"NHI_CLIENT_ID_{agent_type.upper()}") or None

    def get_credential(self, *, client_id: str, agent_type: str) -> Optional[Any]:
        return None  # offline: no cloud credential


class LocalLLMGateway:
    """Cloud-neutral gateway — resolves to an offline, no-egress mode (no key
    leaves the process). Stamps the per-agent attribution headers."""

    def resolve(self, *, agent_type: str, client_id: str, secret_provider: Optional[Any] = None) -> EgressResolution:
        return EgressResolution(
            endpoint="", mode="local-offline", api_key="",
            default_headers={"x-agent-type": agent_type, "x-nhi-id": client_id},
        )


class LocalTraceExporterFactory:
    def create_span_exporter(self) -> Optional[Any]:
        return None  # no exporter — OTel no-ops locally


class LocalProvider:
    """CloudProvider impl: cloud-neutral, fully offline."""

    name = "local"

    def secret_provider(self, **kwargs: Any):
        return EnvVarSecretProvider(**kwargs)

    def identity_provider(self):
        return LocalIdentityProvider()

    def trace_exporter_factory(self):
        return LocalTraceExporterFactory()

    def llm_gateway(self):
        return LocalLLMGateway()

    def runtime_adapter(self):
        return None

    async def audit_backend(self, run_id: str):
        from cloud_adapters.local.audit import LocalHashChainBackend
        return await LocalHashChainBackend.create(run_id=run_id)

    def egress_config_path(self) -> Optional[Path]:
        return _EGRESS_YAML if _EGRESS_YAML.exists() else None


PROVIDER = LocalProvider()
