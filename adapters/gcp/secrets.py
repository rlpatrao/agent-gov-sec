"""
adapters.gcp.secrets — GCP SecretProvider (Secret Manager + ADC).

``SecretManagerProvider`` implements ``core.interfaces.SecretProvider``
(``get_api_key`` / ``invalidate``). It is the GCP managed-secret path; the
agnostic env-var fallback lives in ``core.secrets.EnvVarSecretProvider`` and is
also used here when Secret Manager is unreachable.

In GKE / Cloud Run:
  - The workload's Service Account (via Workload Identity / ADC) grants
    ``secretmanager.versions.access``
  - ``google-cloud-secret-manager`` picks up ADC automatically (no static keys)
  - The fetched value is cached and refreshed every 5 minutes

Locally (dev):
  - Falls back to the env var named by ``env_var_fallback`` if the SDK/ADC is unreachable

The Google SDK import is lazy/guarded so importing this module without
``google-cloud-secret-manager`` installed degrades to env-var mode.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SecretManagerProvider:
    """Short-lived credential provider backed by GCP Secret Manager.

    ``secret_name`` is the secret id; the latest version is accessed. All callers
    use ``get_api_key()`` — never reading env vars directly.
    """

    _TTL_SECONDS = 300

    def __init__(
        self,
        secret_name: str = "galaxy-llm-api-key",
        env_var_fallback: str = "GCP_LLM_API_KEY",
        project: Optional[str] = None,
        version: str = "latest",
    ) -> None:
        self._secret_name = secret_name
        self._env_var = env_var_fallback
        self._project = (
            project
            or os.environ.get("GOOGLE_SECRET_MANAGER_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        self._version = version
        self._cached_key: Optional[str] = None
        self._fetched_at: float = 0.0
        self._client: Optional[object] = None

        try:
            from google.cloud import secretmanager
            self._client = secretmanager.SecretManagerServiceClient()
            logger.info("gcp_secret.client_ready", extra={"project": self._project})
        except ImportError:
            logger.warning("gcp_secret.sdk_missing — env-var fallback only")
        except Exception as e:  # ADC/project resolution can fail locally
            logger.warning("gcp_secret.client_unavailable", extra={"error": str(e)})

    def get_api_key(self) -> str:
        now = time.monotonic()
        if self._cached_key and (now - self._fetched_at) < self._TTL_SECONDS:
            return self._cached_key

        if self._client is not None and self._project:
            try:
                name = f"projects/{self._project}/secrets/{self._secret_name}/versions/{self._version}"
                resp = self._client.access_secret_version(request={"name": name})
                value = resp.payload.data.decode("utf-8")
                if value:
                    self._cached_key = value
                    self._fetched_at = now
                    logger.info("gcp_secret.refreshed", extra={"source": "secret_manager"})
                    return value
            except Exception as e:
                logger.error("gcp_secret.fetch_failed", extra={"error": str(e), "secret": self._secret_name})
                if self._cached_key:
                    logger.warning("gcp_secret.using_stale_cached_key")
                    return self._cached_key

        env_key = os.environ.get(self._env_var)
        if env_key:
            self._cached_key = env_key
            self._fetched_at = now
            logger.info("gcp_secret.refreshed", extra={"source": "env_var"})
            return env_key

        raise EnvironmentError(
            f"No API key available (secret={self._secret_name}, env={self._env_var}). "
            f"In GKE/Cloud Run: check the SA's secretmanager.versions.access permission. "
            f"Locally: set {self._env_var} in .env"
        )

    def invalidate(self) -> None:
        """Force a refresh on next call — call this on 401/expired responses."""
        self._cached_key = None
        self._fetched_at = 0.0
        logger.info("gcp_secret.cache_invalidated")
