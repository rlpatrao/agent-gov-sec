"""
adapters.aws.secrets — AWS SecretProvider (Secrets Manager + SSM Parameter Store).

``SecretsManagerProvider`` implements ``core.interfaces.SecretProvider``
(``get_api_key`` / ``invalidate``). It is the AWS managed-secret path; the
agnostic env-var fallback lives in ``core.secrets.EnvVarSecretProvider`` and is
also used here when Secrets Manager / SSM are unreachable.

In ECS/EKS:
  - The task/pod role (IRSA) grants ``secretsmanager:GetSecretValue`` / ``ssm:GetParameter``
  - boto3's default credential chain picks up the role automatically (no static keys)
  - The fetched value is cached and refreshed every 5 minutes

Locally (dev):
  - Falls back to the env var named by ``env_var_fallback`` if boto3/AWS is unreachable

The ``boto3`` import is lazy/guarded so importing this module without the AWS
SDK installed degrades to env-var mode rather than failing.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SecretsManagerProvider:
    """Short-lived credential provider backed by AWS Secrets Manager / SSM.

    ``source`` selects the store: ``"secretsmanager"`` (default) or ``"ssm"``.
    All callers use ``get_api_key()`` — never reading env vars directly.
    """

    _TTL_SECONDS = 300

    def __init__(
        self,
        secret_name: str = "galaxy/llm-api-key",
        env_var_fallback: str = "AWS_LLM_API_KEY",
        region: Optional[str] = None,
        source: str = "secretsmanager",
    ) -> None:
        self._secret_name = secret_name
        self._env_var = env_var_fallback
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._source = source
        self._cached_key: Optional[str] = None
        self._fetched_at: float = 0.0
        self._client: Optional[object] = None

        try:
            import boto3  # noqa: F401
            self._client = boto3.client(self._source, region_name=self._region)
            logger.info("aws_secret.client_ready", extra={"source": source, "region": self._region})
        except ImportError:
            logger.warning("aws_secret.boto3_missing — env-var fallback only")
        except Exception as e:  # credential/region resolution can fail locally
            logger.warning("aws_secret.client_unavailable", extra={"error": str(e)})

    def get_api_key(self) -> str:
        now = time.monotonic()
        if self._cached_key and (now - self._fetched_at) < self._TTL_SECONDS:
            return self._cached_key

        if self._client is not None:
            try:
                if self._source == "ssm":
                    resp = self._client.get_parameter(Name=self._secret_name, WithDecryption=True)
                    value = resp["Parameter"]["Value"]
                else:
                    resp = self._client.get_secret_value(SecretId=self._secret_name)
                    value = resp.get("SecretString") or ""
                if value:
                    self._cached_key = value
                    self._fetched_at = now
                    logger.info("aws_secret.refreshed", extra={"source": self._source})
                    return value
            except Exception as e:
                logger.error("aws_secret.fetch_failed", extra={"error": str(e), "secret": self._secret_name})
                if self._cached_key:
                    logger.warning("aws_secret.using_stale_cached_key")
                    return self._cached_key

        env_key = os.environ.get(self._env_var)
        if env_key:
            self._cached_key = env_key
            self._fetched_at = now
            logger.info("aws_secret.refreshed", extra={"source": "env_var"})
            return env_key

        raise EnvironmentError(
            f"No API key available (secret={self._secret_name}, env={self._env_var}). "
            f"In ECS/EKS: check the task role's secretsmanager/ssm permissions. "
            f"Locally: set {self._env_var} in .env"
        )

    def invalidate(self) -> None:
        """Force a refresh on next call — call this on 401/expired responses."""
        self._cached_key = None
        self._fetched_at = 0.0
        logger.info("aws_secret.cache_invalidated")
