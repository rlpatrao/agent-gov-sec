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
    # LangGraph demo payload (finops/auditor/rogue). Local-dev defaults are
    # non-empty so the offline demo runs with no env config; in a real tenant
    # each maps to its own Entra App / IAM role / GCP SA via NHI_CLIENT_ID_*.
    "FinOps":               os.environ.get("NHI_CLIENT_ID_FINOPS",               "local-finops-nhi"),
    "Auditor":              os.environ.get("NHI_CLIENT_ID_AUDITOR",              "local-auditor-nhi"),
    "Rogue":                os.environ.get("NHI_CLIENT_ID_ROGUE",                "local-rogue-nhi"),
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
