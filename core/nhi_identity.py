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
    # Shared / Step 0
    "Classifier":           os.environ.get("NHI_CLIENT_ID_CLASSIFIER",          ""),
    # Migration pipeline
    "Scanner":              os.environ.get("NHI_CLIENT_ID_SCANNER",              ""),
    "ASTAnalyzer":          os.environ.get("NHI_CLIENT_ID_ASTANALYZER",          ""),
    "Analyzer":             os.environ.get("NHI_CLIENT_ID_ANALYZER",             ""),
    "LambdaAnalyzer":       os.environ.get("NHI_CLIENT_ID_LAMBDAANALYZER",       ""),
    "Architect":            os.environ.get("NHI_CLIENT_ID_ARCHITECT",            ""),
    "Coder":                os.environ.get("NHI_CLIENT_ID_CODER",                ""),
    "Reviewer":             os.environ.get("NHI_CLIENT_ID_REVIEWER",             ""),
    "Security":             os.environ.get("NHI_CLIENT_ID_SECURITY",             ""),
    "SecurityReviewer":     os.environ.get("NHI_CLIENT_ID_SECURITYREVIEWER",     ""),
    "Tester":               os.environ.get("NHI_CLIENT_ID_TESTER",               ""),
    "IaCGen":               os.environ.get("NHI_CLIENT_ID_IACGEN",               ""),
    "SLOWatcher":           os.environ.get("NHI_CLIENT_ID_SLOWATCHER",           ""),
    # Discovery pipeline
    "DiscoveryScanner":     os.environ.get("NHI_CLIENT_ID_DISCOVERYSCANNER",     ""),
    "DiscoveryGrapher":     os.environ.get("NHI_CLIENT_ID_DISCOVERYGRAPHER",     ""),
    "DiscoveryBRD":         os.environ.get("NHI_CLIENT_ID_DISCOVERYBRD",         ""),
    "DiscoveryArchitect":   os.environ.get("NHI_CLIENT_ID_DISCOVERYARCHITECT",   ""),
    "DiscoveryStories":     os.environ.get("NHI_CLIENT_ID_DISCOVERYSTORIES",     ""),
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
        import re
        _UUID_RE = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        missing = [
            agent for agent, client_id in _NHI_CLIENT_IDS.items()
            if not _UUID_RE.match(client_id)
        ]
        if missing:
            logger.warning(
                "nhi.missing_real_entra_ids",
                extra={"agents": missing},
            )
            # Warning only in local dev. In ACA each job's MI injects the real UUID.
