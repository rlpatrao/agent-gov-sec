"""
core.interfaces — the cloud- and framework-agnostic seam.

Every cloud-specific binding (Azure today; AWS/GCP planned) is expressed as
one of the Protocols below and lives under ``cloud_adapters/<cloud>/``. The agnostic
core (``core/``, ``governance/``, ``a2a/``) depends only on these Protocols,
never on a cloud SDK or an agent framework.

Nothing here imports ``azure.*`` or ``agent_framework`` — that is the
invariant the WS1 refactor exists to enforce. ``AuditBackend`` is re-exported
from MSGK (``agent_os``) so adapters implement the upstream interface directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

# Re-export MSGK's audit interface so cloud adapters implement the upstream
# contract (Azure Postgres, AWS DynamoDB, GCP BigQuery all subclass this).
from agent_os.audit_logger import AuditBackend  # noqa: F401  (re-export)

__all__ = [
    "AuditBackend",
    "SecretProvider",
    "IdentityProvider",
    "TraceExporterFactory",
    "EgressResolution",
    "LLMGateway",
    "AgentRuntimeAdapter",
    "CloudProvider",
]


@runtime_checkable
class SecretProvider(Protocol):
    """Resolves a single API key/secret, with a managed-store path and an
    env-var fallback. Azure → Key Vault; AWS → Secrets Manager; GCP → Secret
    Manager. The agnostic default (``core.secrets.EnvVarSecretProvider``) is
    env-var only."""

    def get_api_key(self) -> str: ...
    def invalidate(self) -> None: ...


@runtime_checkable
class IdentityProvider(Protocol):
    """Resolves an agent's Non-Human Identity against the cloud's identity system.

    Two responsibilities:
      - ``resolve_client_id`` — map an agent *type* to its cloud **principal id**,
        sourced from the cloud directory: Azure → Entra (App Registration /
        User-Assigned Managed Identity ``clientId``); AWS → IAM (role ARN);
        GCP → the Service Account email. The standard bridge is an
        ``NHI_CLIENT_ID_<AGENT_TYPE>`` env var that IaC populates from the
        directory; adapters may also resolve live. Returns ``None`` if unknown.
      - ``get_credential`` — exchange that principal id for a usable credential
        (Azure ManagedIdentityCredential, AWS STS AssumeRole, GCP WIF). ``None``
        in local dev where no cloud identity is available.
    """

    def resolve_client_id(self, *, agent_type: str) -> Optional[str]: ...
    def get_credential(self, *, client_id: str, agent_type: str) -> Optional[Any]: ...


@runtime_checkable
class TraceExporterFactory(Protocol):
    """Builds the OTel span exporter for a cloud's tracing backend.
    Azure → Azure Monitor; AWS → X-Ray/ADOT; GCP → Cloud Trace. Returns
    ``None`` when the exporter or its config is unavailable."""

    def create_span_exporter(self) -> Optional[Any]: ...


@dataclass(frozen=True)
class EgressResolution:
    """The resolved LLM-egress chokepoint: which endpoint to call, in which
    mode, and the headers/key to present."""

    endpoint: str
    mode: str                                   # e.g. "apim" | "aoai-direct"
    api_key: str
    default_headers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class LLMGateway(Protocol):
    """The sole managed LLM-egress path. Azure → APIM → Azure OpenAI (direct
    AOAI fallback); AWS → API Gateway → Bedrock; GCP → Apigee → Vertex AI.
    Pairs with the per-cloud egress allow-list."""

    def resolve(
        self,
        *,
        agent_type: str,
        client_id: str,
        secret_provider: Optional[SecretProvider] = None,
    ) -> EgressResolution: ...


@runtime_checkable
class AgentRuntimeAdapter(Protocol):
    """The agent-framework binding seam. Azure ships the MAF adapter; AWS/GCP
    would wire LangGraph/Bedrock-Agents or Google ADK. ``configure_observability``
    lets the framework own OTel provider setup so its semantic-convention spans
    fire; returns True if it handled setup (else the agnostic fallback runs)."""

    def configure_observability(self, exporters: Optional[list[Any]]) -> bool: ...


class CloudProvider(Protocol):
    """Umbrella resolved by ``core.provider_factory.get_provider()``. One impl
    per cloud under ``cloud_adapters/<cloud>/``. AWS/GCP impls raise
    ``NotImplementedError`` from each accessor until WS5/WS6 fill them in."""

    name: str

    def secret_provider(self, **kwargs: Any) -> SecretProvider: ...
    def identity_provider(self) -> IdentityProvider: ...
    def trace_exporter_factory(self) -> TraceExporterFactory: ...
    def llm_gateway(self) -> LLMGateway: ...
    def runtime_adapter(self) -> Optional[AgentRuntimeAdapter]: ...
    async def audit_backend(self, run_id: str) -> AuditBackend: ...
    def egress_config_path(self) -> Optional[Path]: ...
