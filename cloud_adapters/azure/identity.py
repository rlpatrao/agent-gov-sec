"""
cloud_adapters.azure.identity — Azure IdentityProvider.

Resolves a ManagedIdentityCredential scoped to an agent's NHI client id. In
AKS/ACA the credential picks up the pod's Workload Identity federated OIDC
token; no secret material is involved. Returns ``None`` in local dev where the
Azure SDK or a managed identity is unavailable.

The ``azure.identity`` import is lazy (inside the method) so merely importing
the Azure adapter package does not require the Azure SDK.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AzureIdentityProvider:
    """IdentityProvider impl backed by Entra Managed Identity."""

    def resolve_client_id(self, *, agent_type: str) -> Optional[str]:
        """Resolve the agent's **Entra** principal id (App Registration /
        User-Assigned Managed Identity ``clientId``).

        Source of truth is Entra. The standard runtime bridge is the
        ``NHI_CLIENT_ID_<AGENT_TYPE>`` env var, which IaC (Bicep/Terraform)
        populates from the Entra object it creates for the agent — so the value
        *originates in Entra*, IaC carries it to the pod. When the env isn't set,
        an optional live Entra lookup (by the ``galaxy-<agent_type>`` naming
        convention) can be enabled — see ``_resolve_from_entra``.
        """
        env = os.environ.get(f"NHI_CLIENT_ID_{agent_type.upper()}")
        if env:
            return env
        return self._resolve_from_entra(agent_type)

    def _resolve_from_entra(self, agent_type: str) -> Optional[str]:
        """Optional live resolution: look up the App Registration / User-Assigned
        MI named ``galaxy-<agent_type>`` in Entra and return its ``clientId``.

        Opt-in via ``GALAXY_ENTRA_LOOKUP=1`` (needs ``azure-identity`` +
        Microsoft Graph access with ``Application.Read.All`` and a configured
        tenant). Off by default and degrades to ``None`` — the env bridge above
        is the standard path. This is the documented extension point; wire the
        Graph call here when live discovery is wanted.
        """
        if os.environ.get("GALAXY_ENTRA_LOOKUP", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        logger.info("azure.entra_lookup_not_wired", extra={"agent_type": agent_type})
        return None  # extension point — see docstring

    def get_credential(self, *, client_id: str, agent_type: str) -> Optional[Any]:
        if not client_id:
            return None
        try:
            from azure.identity import ManagedIdentityCredential
        except ImportError:
            logger.warning("azure.identity_sdk_missing", extra={"agent_type": agent_type})
            return None
        try:
            return ManagedIdentityCredential(client_id=client_id)
        except Exception as e:
            logger.warning(
                "nhi.credential_unavailable",
                extra={"agent_type": agent_type, "error": str(e)},
            )
            return None
