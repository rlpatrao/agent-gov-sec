"""
adapters.azure.secrets — Azure SecretProvider (Key Vault + Workload Identity).

``TokenProvider`` implements ``core.interfaces.SecretProvider`` (``get_api_key``
/ ``invalidate``). It is the Azure managed-secret path; the agnostic env-var
fallback lives in ``core.secrets.EnvVarSecretProvider`` and is also used here
when Key Vault is unreachable.

In AKS/ACA:
  - Pod has a federated token via Workload Identity (no secrets in env vars)
  - DefaultAzureCredential picks up the federated token automatically
  - Key Vault issues a short-lived secret fetch — cached and refreshed every 5 minutes

Locally (dev):
  - Falls back to the env-var named by `env_var_fallback` if Key Vault is unreachable
  - Never use this fallback in staging or prod

The ``azure.*`` imports are guarded; importing this module without the Azure
SDK installed degrades to env-var mode rather than failing.
"""

import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports so local dev without Azure SDK installed still works for tests
try:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    from azure.core.exceptions import AzureError
    _AZURE_AVAILABLE = True
except ImportError:
    _AZURE_AVAILABLE = False
    logger.warning("azure-identity not installed — Key Vault unavailable, using env var fallback")


class TokenProvider:
    """
    Short-lived credential provider.

    All agent code calls get_api_key() — never reads env vars directly.
    Token is cached and refreshed before expiry.
    """

    _TTL_SECONDS = 300          # refresh every 5 minutes

    def __init__(
        self,
        vault_url: Optional[str] = None,
        secret_name: str = "azure-openai-key",
        env_var_fallback: str = "AZURE_OPENAI_KEY",
    ):
        self._vault_url = vault_url or os.environ.get("AZURE_KEY_VAULT_URL")
        self._secret_name = secret_name
        self._env_var = env_var_fallback
        self._cached_key: Optional[str] = None
        self._fetched_at: float = 0
        self._client: Optional[object] = None

        if self._vault_url and _AZURE_AVAILABLE:
            try:
                credential = DefaultAzureCredential()
                self._client = SecretClient(
                    vault_url=self._vault_url,
                    credential=credential,
                )
                logger.info("token_provider.keyvault_connected", extra={"vault": self._vault_url})
            except Exception as e:
                logger.warning(
                    "token_provider.keyvault_unavailable",
                    extra={"error": str(e)},
                )
        else:
            logger.info("token_provider.local_mode", extra={"env_var": self._env_var})

    def get_api_key(self) -> str:
        """
        Returns a valid API key.
        Fetches from Key Vault if available, falls back to env var for local dev.
        Raises EnvironmentError if neither is available.
        """
        now = time.monotonic()

        # Cache hit — return without a Key Vault round-trip
        if self._cached_key and (now - self._fetched_at) < self._TTL_SECONDS:
            return self._cached_key

        # Key Vault path (AKS production)
        if self._client:
            try:
                secret = self._client.get_secret(self._secret_name)
                self._cached_key = secret.value
                self._fetched_at = now
                logger.info("token_provider.refreshed", extra={"source": "keyvault", "secret": self._secret_name})
                return self._cached_key
            except Exception as e:
                logger.error("token_provider.keyvault_fetch_failed", extra={"error": str(e), "secret": self._secret_name})
                # Fall through to env var if cached key still exists
                if self._cached_key:
                    logger.warning("token_provider.using_stale_cached_key")
                    return self._cached_key

        # Local dev fallback
        env_key = os.environ.get(self._env_var)
        if env_key:
            self._cached_key = env_key
            self._fetched_at = now
            logger.info("token_provider.refreshed", extra={"source": "env_var"})
            return env_key

        raise EnvironmentError(
            f"No API key available (secret={self._secret_name}, env={self._env_var}). "
            "In AKS: check Workload Identity and Key Vault access. "
            f"Locally: set {self._env_var} in .env"
        )

    def invalidate(self) -> None:
        """Force a refresh on next call — call this on 401 responses."""
        self._cached_key = None
        self._fetched_at = 0
        logger.info("token_provider.cache_invalidated")
