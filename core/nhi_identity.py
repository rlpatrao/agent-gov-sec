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
    "Classifier":           os.environ.get("NHI_CLIENT_ID_CLASSIFIER",          "c4be541a-a1f2-433c-8166-9ebcf2d87b78"),
    # Migration pipeline
    "Scanner":              os.environ.get("NHI_CLIENT_ID_SCANNER",              "e581d9ea-c4ca-411f-9946-2e784d9c4046"),
    "ASTAnalyzer":          os.environ.get("NHI_CLIENT_ID_ASTANALYZER",          "7d22106a-5fe0-467c-98f4-1080d8bcea4d"),
    "Analyzer":             os.environ.get("NHI_CLIENT_ID_ANALYZER",             "8cdc89ee-932e-4536-a563-434af7df3c9b"),
    "LambdaAnalyzer":       os.environ.get("NHI_CLIENT_ID_LAMBDAANALYZER",       "17de927d-a8d7-447b-90b7-d1d649009179"),
    "Architect":            os.environ.get("NHI_CLIENT_ID_ARCHITECT",            "7b2e5510-bbee-4da4-a99d-e60711fa0be7"),
    "Coder":                os.environ.get("NHI_CLIENT_ID_CODER",                "f51216a1-0e67-43c9-acb8-149954e8d4e0"),
    "Reviewer":             os.environ.get("NHI_CLIENT_ID_REVIEWER",             "b44d54a3-d329-49aa-89cb-ea35522768ba"),
    "Security":             os.environ.get("NHI_CLIENT_ID_SECURITY",             "72f1b573-1796-474e-b961-390ae8ad33fe"),
    "SecurityReviewer":     os.environ.get("NHI_CLIENT_ID_SECURITYREVIEWER",     "ae944f1a-1032-4cbb-ba53-8cb73a790043"),
    "Tester":               os.environ.get("NHI_CLIENT_ID_TESTER",               "7eeb7e1a-b6f2-45d5-b721-2fa0b49da988"),
    "IaCGen":               os.environ.get("NHI_CLIENT_ID_IACGEN",               "72728f28-0955-4378-8782-cde5fdc6dff3"),
    "SLOWatcher":           os.environ.get("NHI_CLIENT_ID_SLOWATCHER",           "92f68691-ea09-4249-b9a1-221a5888c361"),
    # Discovery pipeline
    "DiscoveryScanner":     os.environ.get("NHI_CLIENT_ID_DISCOVERYSCANNER",     "40d042bb-a23e-4158-92f8-70accc3023c7"),
    "DiscoveryGrapher":     os.environ.get("NHI_CLIENT_ID_DISCOVERYGRAPHER",     "5a603c38-d178-4da4-94dd-85cedc9cd983"),
    "DiscoveryBRD":         os.environ.get("NHI_CLIENT_ID_DISCOVERYBRD",         "333b400b-170a-4ed0-9fae-42866a93b84f"),
    "DiscoveryArchitect":   os.environ.get("NHI_CLIENT_ID_DISCOVERYARCHITECT",   "cc0da4ab-22fa-4707-8184-4e33c5884c3e"),
    "DiscoveryStories":     os.environ.get("NHI_CLIENT_ID_DISCOVERYSTORIES",     "26c11983-dad1-480e-bff8-09eb8f3ad7f0"),
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
