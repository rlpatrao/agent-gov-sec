"""
cloud_adapters.gcp.identity — GCP IdentityProvider (WS6).

Resolves a per-agent **Service Account** identity. The agent's ``client_id`` is
its dedicated SA email; in production the workload runs under Workload Identity
Federation (GKE WI / ADC) and may impersonate the per-agent SA so each agent
acts under its own least-privilege identity (the GCP analogue of a per-agent
Entra service principal / per-agent IAM role).

The ``google-auth`` / ``google-cloud-iam-credentials`` imports are lazy (inside
the method) so merely importing the GCP adapter package needs no Google SDK.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _derive_enabled() -> bool:
    """Whether to derive a per-agent SA email by naming convention when no
    explicit NHI_CLIENT_ID_* is set. Opt-in via NHI_DERIVE_FROM_CONVENTION."""
    return os.environ.get("NHI_DERIVE_FROM_CONVENTION", "").strip().lower() in ("1", "true", "yes")


class GcpIdentityProvider:
    """IdentityProvider impl backed by per-agent Service Accounts (+ WIF/ADC)."""

    def resolve_client_id(self, *, agent_type: str) -> Optional[str]:
        """Resolve the agent's **Service Account** principal id (its SA email).

        Standard bridge: the ``NHI_CLIENT_ID_<AGENT_TYPE>`` env var that IaC
        (Terraform / Deployment Manager) sets to the SA email it provisions. If
        unset, derive it from the per-agent convention
        ``galaxy-<agent_type>@<project>.iam.gserviceaccount.com`` when
        ``GOOGLE_CLOUD_PROJECT`` is configured. Returns ``None`` if neither is
        available.
        """
        env = os.environ.get(f"NHI_CLIENT_ID_{agent_type.upper()}")
        if env:
            return env
        # Convention-based derivation (galaxy-<agent>@<project>.iam…) is opt-in:
        # it would otherwise mint a plausible SA email for *any* agent name. Off
        # by default, identities come only from explicit NHI_CLIENT_ID_* (set by
        # IaC), so an unprovisioned/unknown agent resolves to None (fail-closed).
        if _derive_enabled():
            project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_SECRET_MANAGER_PROJECT")
            if project:
                return f"galaxy-{agent_type.lower()}@{project}.iam.gserviceaccount.com"
        return None

    def get_credential(self, *, client_id: str, agent_type: str) -> Optional[Any]:
        """Return a short-lived credential for the agent's SA.

        Uses Application Default Credentials and impersonates ``client_id`` (the
        per-agent SA email) via the IAM Credentials API, mirroring the AWS
        provider's STS AssumeRole. Returns ``None`` in local dev where ADC or the
        Google auth libraries are unavailable.
        """
        if not client_id:
            return None
        try:
            import google.auth
            from google.auth import impersonated_credentials
        except ImportError:
            logger.warning("gcp.google_auth_missing", extra={"agent_type": agent_type})
            return None
        try:
            source_credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            return impersonated_credentials.Credentials(
                source_credentials=source_credentials,
                target_principal=client_id,
                target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
                lifetime=3600,
            )
        except Exception as e:
            logger.warning(
                "nhi.credential_unavailable",
                extra={"agent_type": agent_type, "error": str(e)},
            )
            return None
