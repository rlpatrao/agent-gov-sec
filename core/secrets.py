"""
core.secrets — the agnostic, env-var-only SecretProvider.

This is the cloud-neutral default: it reads the key from an environment
variable and caches it. Managed-secret-store providers (Azure Key Vault, AWS
Secrets Manager, GCP Secret Manager) live under ``adapters/<cloud>/secrets.py``
and add a managed path *in front of* this env-var fallback.

Importing this module pulls no cloud SDK.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class EnvVarSecretProvider:
    """SecretProvider backed solely by an environment variable.

    Implements the same ``get_api_key()`` / ``invalidate()`` contract as the
    cloud adapters so callers never branch on provider type. The TTL cache
    keeps the call cheap and the refresh semantics identical to the managed
    providers.
    """

    _TTL_SECONDS = 300

    def __init__(self, env_var_fallback: str = "AZURE_OPENAI_KEY", **_ignored) -> None:
        self._env_var = env_var_fallback
        self._cached_key: Optional[str] = None
        self._fetched_at: float = 0.0
        logger.info("secret_provider.env_mode", extra={"env_var": self._env_var})

    def get_api_key(self) -> str:
        now = time.monotonic()
        if self._cached_key and (now - self._fetched_at) < self._TTL_SECONDS:
            return self._cached_key
        env_key = os.environ.get(self._env_var)
        if env_key:
            self._cached_key = env_key
            self._fetched_at = now
            return env_key
        raise EnvironmentError(
            f"No API key available (env={self._env_var}). "
            f"Set {self._env_var} in your environment/.env, or select a cloud "
            "provider with a managed secret store via CLOUD_PROVIDER."
        )

    def invalidate(self) -> None:
        self._cached_key = None
        self._fetched_at = 0.0
