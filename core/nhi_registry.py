"""
nhi_registry.py

Non-Human Identity (NHI) registry — agnostic. Each agent type maps to its own
cloud identity principal (Entra App Registration / AWS IAM role / GCP Service
Account). This module holds only the agent-type → client-id mapping and the
attribution model; it knows nothing about any cloud SDK.

Resolving an actual cloud *credential* for an identity is the job of an
``IdentityProvider`` under ``adapters/<cloud>/identity.py`` (Azure →
ManagedIdentityCredential, AWS → STS AssumeRole, GCP → Workload Identity
Federation), obtained via ``core.provider_factory.get_provider()``.

CLIENT_IDs are not secrets — they are identity references. Store them in env
vars or config, not a secret store.

Extensibility (open/closed): the registry resolves an agent type from the
static map **or** an ``NHI_CLIENT_ID_<AGENT_TYPE>`` env var, so payload/demo
agent types register by setting their env var — **without editing this core
file**. Keep new-agent registration out of ``core/`` and in env/config (a
payload package can set import-time defaults; see ``payload_agents``).

Significance:
  - Every action in the trace ledger has an nhi_id attached
  - The cloud's audit log shows per-agent activity independently
  - If an agent is compromised, its identity can be disabled without affecting others
  - Satisfies "least privilege" — each NHI has scoped permissions only
"""

import os
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# NO agent types are hardcoded here. The registry is fully generic: it resolves
# any agent type at call time through the selected cloud IdentityProvider
# (Azure → Entra, AWS → IAM, GCP → SA), with an `NHI_CLIENT_ID_<AGENT_TYPE>` env
# bridge as the cloud-agnostic fallback. Agents (platform or payload) register by
# their cloud identity / env var — never by editing this file.


@dataclass(frozen=True)
class AgentIdentity:
    """
    Immutable identity for one agent type.
    client_id is written to every trace ledger entry.
    """
    agent_type: str
    client_id: str

    def __str__(self) -> str:
        return f"{self.agent_type}/{self.client_id}"

    def get_credential(self) -> Optional[Any]:
        """
        Returns a cloud credential scoped to this agent's NHI, resolved through
        the selected provider's ``IdentityProvider`` (Azure
        ManagedIdentityCredential, etc.). ``None`` in local dev where no cloud
        identity is available. Agnostic — no cloud SDK is imported here.
        """
        try:
            from core.provider_factory import get_provider
            return get_provider().identity_provider().get_credential(
                client_id=self.client_id, agent_type=self.agent_type
            )
        except Exception as e:
            logger.warning(
                "nhi.credential_unavailable",
                extra={"agent_type": self.agent_type, "error": str(e)},
            )
            return None


class NHIRegistry:
    """
    Factory for AgentIdentity objects.
    Validates that every agent type has a registered NHI before any run starts.
    """

    @staticmethod
    def get(agent_type: str) -> AgentIdentity:
        """Resolve an agent's NHI principal id. The id is sourced from the
        selected cloud IdentityProvider (Azure → Entra, AWS → IAM, GCP → SA);
        the provider's standard implementation reads the IaC-provisioned
        ``NHI_CLIENT_ID_<AGENT_TYPE>`` env. If no provider can resolve it (e.g.
        an unimplemented cloud, or none selected), fall back to that env var
        directly. No agent type is hardcoded in core."""
        client_id = NHIRegistry._resolve_client_id(agent_type)
        if not client_id:
            raise ValueError(
                f"No NHI registered for agent type '{agent_type}'. "
                f"Provision its cloud identity (Entra App Registration / IAM role "
                f"/ GCP SA) and set NHI_CLIENT_ID_{agent_type.upper()} in the env."
            )
        return AgentIdentity(agent_type=agent_type, client_id=client_id)

    @staticmethod
    def _resolve_client_id(agent_type: str) -> str:
        # 1) cloud IdentityProvider — it knows how to source the id from its
        #    directory (Azure → Entra, AWS → IAM, GCP → SA).
        try:
            from core.provider_factory import get_provider
            cid = get_provider().identity_provider().resolve_client_id(agent_type=agent_type)
            if cid:
                return cid
        except Exception:
            pass  # provider unavailable / not implemented → agnostic env fallback
        # 2) agnostic fallback: the NHI_CLIENT_ID_<TYPE> env bridge.
        return os.environ.get(f"NHI_CLIENT_ID_{agent_type.upper()}", "")

    @staticmethod
    def validate_all() -> None:
        """Warn if any configured ``NHI_CLIENT_ID_*`` env var isn't a real cloud
        id (a UUID, for Entra). Generic — validates whatever the environment
        registers; no agent list is hardcoded. In production each id is the
        real Entra App Registration / IAM role / GCP SA injected by IaC."""
        import re
        _UUID_RE = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        placeholders = [
            k[len("NHI_CLIENT_ID_"):] for k, v in os.environ.items()
            if k.startswith("NHI_CLIENT_ID_") and not _UUID_RE.match(v or "")
        ]
        if placeholders:
            logger.warning("nhi.non_uuid_ids", extra={"agents": placeholders})
