"""
nhi_identity.py

Non-Human Identity (NHI) — each agent type has its own Entra service principal.

In production:
  - Each agent's CLIENT_ID is registered in Entra as a dedicated service principal
  - ManagedIdentityCredential picks up the pod's Workload Identity federated token
  - Actions taken by each agent are attributable to its own identity in Entra audit logs
  - The Compliance Auditor can see that Scanner/xxx read files, Coder/xxx wrote code

CLIENT_IDs are not secrets — they are identity references.
Store them in env vars or config, not Key Vault.

Significance:
  - Every action in the trace ledger has an nhi_id attached
  - Entra audit log shows per-agent activity independently
  - If an agent is compromised, it can be disabled in Entra without affecting others
  - Satisfies the "least privilege" requirement — each NHI has scoped permissions only
"""

import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from azure.identity import ManagedIdentityCredential
    _AZURE_AVAILABLE = True
except ImportError:
    _AZURE_AVAILABLE = False


# Registry of all agent NHI client IDs
# In AKS: loaded from env vars set by Bicep/Terraform during provisioning
# In local dev: placeholder values are fine — no real auth happens
_NHI_CLIENT_IDS: dict[str, str] = {
    "Scanner":   os.environ.get("NHI_CLIENT_ID_SCANNER",   "local-scanner-nhi"),
    "Architect": os.environ.get("NHI_CLIENT_ID_ARCHITECT", "local-architect-nhi"),
    "Coder":     os.environ.get("NHI_CLIENT_ID_CODER",     "local-coder-nhi"),
    "Reviewer":  os.environ.get("NHI_CLIENT_ID_REVIEWER",  "local-reviewer-nhi"),
    "Security":  os.environ.get("NHI_CLIENT_ID_SECURITY",  "local-security-nhi"),
    "Tester":    os.environ.get("NHI_CLIENT_ID_TESTER",    "local-tester-nhi"),
    "IaCGen":    os.environ.get("NHI_CLIENT_ID_IACGEN",    "local-iacgen-nhi"),
    "SLOWatcher":os.environ.get("NHI_CLIENT_ID_SLOWATCHER","local-slowatcher-nhi"),
}


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

    def get_credential(self) -> Optional[object]:
        """
        Returns a ManagedIdentityCredential scoped to this agent's NHI.
        None in local dev where Azure identity is unavailable.
        """
        if not _AZURE_AVAILABLE:
            return None
        try:
            return ManagedIdentityCredential(client_id=self.client_id)
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
        client_id = _NHI_CLIENT_IDS.get(agent_type)
        if not client_id:
            raise ValueError(
                f"No NHI registered for agent type '{agent_type}'. "
                f"Register it in Entra and add NHI_CLIENT_ID_{agent_type.upper()} "
                f"to your environment config."
            )
        return AgentIdentity(agent_type=agent_type, client_id=client_id)

    @staticmethod
    def validate_all() -> None:
        """
        Call at platform startup to verify all NHIs are configured.
        Fail fast before any agent runs.
        """
        missing = [
            agent for agent, client_id in _NHI_CLIENT_IDS.items()
            if client_id.startswith("local-")
        ]
        if missing:
            logger.warning(
                "nhi.local_placeholders_in_use",
                extra={"agents": missing},
            )
            # Warning only — allows local dev. Raises in prod via a stricter check.
