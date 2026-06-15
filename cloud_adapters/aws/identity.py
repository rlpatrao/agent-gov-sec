"""
cloud_adapters.aws.identity — AWS IdentityProvider.

Resolves a temporary AWS credential scoped to an agent's Non-Human Identity.
The agent's ``client_id`` is interpreted as its dedicated **IAM role ARN**; the
provider assumes that role via STS so the agent acts under its own least-priv
identity (the AWS analogue of a per-agent Entra service principal).

In EKS the pod's ServiceAccount is IRSA-annotated and boto3's default chain
already yields role credentials; STS ``AssumeRole`` then scopes down to the
per-agent role. Returns ``None`` in local dev where the AWS SDK or a base
credential is unavailable.

The ``boto3`` import is lazy (inside the method) so merely importing the AWS
adapter package does not require the AWS SDK.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AwsIdentityProvider:
    """IdentityProvider impl backed by STS AssumeRole (per-agent IAM role)."""

    def resolve_client_id(self, *, agent_type: str) -> Optional[str]:
        """Resolve the agent's **IAM** principal id (its role ARN).

        Standard bridge: the ``NHI_CLIENT_ID_<AGENT_TYPE>`` env var that IaC
        (CDK/Terraform) sets to the role ARN it provisions. If unset *and*
        ``NHI_DERIVE_FROM_CONVENTION`` is enabled, derive it from the per-agent
        role-name convention ``galaxy-<agent_type>`` when ``AWS_ACCOUNT_ID`` is
        configured (the role-name pattern the WS5 Terraform creates). Returns
        ``None`` otherwise — an unknown agent fails closed.
        """
        env = os.environ.get(f"NHI_CLIENT_ID_{agent_type.upper()}")
        if env:
            return env
        # Convention-based derivation (arn:…:role/galaxy-<agent>) is opt-in: it
        # would otherwise mint a plausible role ARN for *any* agent name. Off by
        # default, identities come only from explicit NHI_CLIENT_ID_* (set by
        # IaC), so an unprovisioned/unknown agent resolves to None (fail-closed).
        if os.environ.get("NHI_DERIVE_FROM_CONVENTION", "").strip().lower() in ("1", "true", "yes"):
            account = os.environ.get("AWS_ACCOUNT_ID")
            if account:
                return f"arn:aws:iam::{account}:role/galaxy-{agent_type.lower()}"
        return None

    def get_credential(self, *, client_id: str, agent_type: str) -> Optional[Any]:
        # client_id == the agent's IAM role ARN (e.g. arn:aws:iam::123:role/galaxy-analyzer)
        if not client_id:
            return None
        try:
            import boto3
        except ImportError:
            logger.warning("aws.boto3_missing", extra={"agent_type": agent_type})
            return None
        try:
            sts = boto3.client("sts", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            resp = sts.assume_role(
                RoleArn=client_id,
                RoleSessionName=f"galaxy-{agent_type}"[:64],
            )
            # Return the temporary-credentials dict; callers build a session from it.
            return resp.get("Credentials")
        except Exception as e:
            logger.warning(
                "nhi.credential_unavailable",
                extra={"agent_type": agent_type, "error": str(e)},
            )
            return None
