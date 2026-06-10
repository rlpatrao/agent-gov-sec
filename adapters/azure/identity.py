"""
adapters.azure.identity — Azure IdentityProvider.

Resolves a ManagedIdentityCredential scoped to an agent's NHI client id. In
AKS/ACA the credential picks up the pod's Workload Identity federated OIDC
token; no secret material is involved. Returns ``None`` in local dev where the
Azure SDK or a managed identity is unavailable.

The ``azure.identity`` import is lazy (inside the method) so merely importing
the Azure adapter package does not require the Azure SDK.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AzureIdentityProvider:
    """IdentityProvider impl backed by Entra Managed Identity."""

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
