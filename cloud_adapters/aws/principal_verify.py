"""
cloud_adapters/aws/principal_verify.py — AWS implementation of PrincipalVerifier.

Verifies that a claimed agent principal (an IAM role) actually exists and that the
caller asking to enroll it is a human principal, using **read-only** AWS APIs
only: ``sts:GetCallerIdentity`` and ``iam:GetRole``.

This adapter deliberately provides no way to create or modify an identity. The
Governance Authority's execution role needs nothing beyond those two read
permissions; granting it ``iam:CreateRole`` or ``iam:PutRolePolicy`` would let the
service that enforces policy mint a privileged principal for itself. Agent roles
are created by Terraform (``cloud_adapters/aws/infra/main.tf``) or by the
developer's own SSO session, out of band.

Credentials come from the ambient AWS chain, so a developer runs::

    aws sso login --profile <profile>
    AWS_PROFILE=<profile> galaxy enroll Payroll

and the enrollment is attributed to their SSO identity in the audit record.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from governance.remote.registrar import CallerIdentity, EnrollmentDenied, VerifiedPrincipal

logger = logging.getLogger(__name__)

# arn:aws:iam::<account>:role/<path?><name>
_ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::(?P<account>\d{12}):role/(?P<name>.+)$")


class AwsPrincipalVerifier:
    """``PrincipalVerifier`` backed by STS and IAM reads.

    ``region`` only affects endpoint selection; IAM is global. boto3 is imported
    lazily so the module (and the Registrar that type-checks against it) imports
    without the AWS extra installed.
    """

    def __init__(self, region: Optional[str] = None, profile: Optional[str] = None) -> None:
        self.region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self.profile = profile or os.environ.get("AWS_PROFILE")
        self._session = None

    def _boto_session(self):
        if self._session is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover - requires the [aws] extra
                raise EnrollmentDenied(
                    "boto3 is not installed; install the AWS extra (pip install '.[aws]') "
                    "to enroll an AWS principal"
                ) from e
            kwargs = {"region_name": self.region}
            if self.profile:
                kwargs["profile_name"] = self.profile
            self._session = boto3.Session(**kwargs)
        return self._session

    def caller_identity(self) -> CallerIdentity:
        """Resolve the calling principal via ``sts:GetCallerIdentity``.

        A missing or expired credential is reported as a denial with the SSO
        remediation, because that is the common first-run case.
        """
        try:
            ident = self._boto_session().client("sts").get_caller_identity()
        except EnrollmentDenied:
            raise
        except Exception as e:
            login = "aws sso login" + (f" --profile {self.profile}" if self.profile else "")
            raise EnrollmentDenied(
                f"cannot resolve AWS caller identity ({type(e).__name__}). "
                f"Run `{login}` and retry."
            ) from e
        return CallerIdentity(
            arn=ident.get("Arn", ""),
            account=ident.get("Account", ""),
            user_id=ident.get("UserId", ""),
        )

    def verify_principal(self, *, agent_type: str, principal_id: str) -> VerifiedPrincipal:
        """Confirm the IAM role in ``principal_id`` exists.

        ``principal_id`` may be a full role ARN or a bare role name; a bare name is
        qualified against the caller's account. The role's existence is checked
        with ``iam:GetRole``, and the returned ARN is the one AWS reports rather
        than the one supplied, so a caller cannot record a principal that differs
        from the verified one.
        """
        caller = self.caller_identity()
        arn_match = _ROLE_ARN_RE.match(principal_id)
        if arn_match:
            account, role_name = arn_match.group("account"), arn_match.group("name")
        elif "/" not in principal_id and ":" not in principal_id:
            account, role_name = caller.account, principal_id
        else:
            raise EnrollmentDenied(
                f"{principal_id!r} is not an IAM role ARN or role name "
                f"(expected arn:aws:iam::<account>:role/<name>)"
            )

        if account != caller.account:
            raise EnrollmentDenied(
                f"principal is in account {account} but the caller is authenticated "
                f"to {caller.account}; enroll from the target account"
            )

        try:
            role = self._boto_session().client("iam").get_role(
                RoleName=role_name.rsplit("/", 1)[-1])["Role"]
        except EnrollmentDenied:
            raise
        except Exception as e:
            name = type(e).__name__
            if "NoSuchEntity" in name or "NoSuchEntity" in str(e):
                raise EnrollmentDenied(
                    f"IAM role {role_name!r} does not exist in account {account}. "
                    f"Provision it first (terraform apply in cloud_adapters/aws/infra "
                    f"creates one role per discovered agent type), then re-run enroll."
                ) from e
            raise EnrollmentDenied(
                f"cannot verify IAM role {role_name!r} ({name}). The enroller needs "
                f"read-only iam:GetRole on it."
            ) from e

        logger.info("principal_verify.ok", extra={
            "agent_type": agent_type, "principal_id": role["Arn"], "account": account})
        return VerifiedPrincipal(principal_id=role["Arn"], account=account, cloud="aws")
