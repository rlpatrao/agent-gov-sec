"""
galaxy_gov/remote/registrar.py — the Governance Authority control plane.

The Registrar records ``agent_type → cloud principal`` bindings in the
authority-side :class:`~galaxy_gov.remote.identity_store.IdentityStore`. It is the
control-plane half of the authority; ``galaxy_gov.remote.enforce`` is the data
plane. They are deliberately separate: enforcement runs on every request, while
enrollment is a rare, human-authorized operation.

Two-key model
-------------
An agent can transact only when **both** keys are turned:

1. **Identity enrolled** (this module) — a binding exists and is active. Automated,
   authorized by a human cloud credential (an AWS SSO session), and grants
   nothing on its own.
2. **Policy approved** (``galaxy_gov.policy_export`` → the deployed registry) —
   a reviewed ``ControlPolicy`` exists for the type. This is where capabilities
   live, and it stays a reviewed artifact because the runtime floor does not
   clamp ``allowed_tools`` or ``allowed_recipients``.

Enrolling a type whose policy has not been approved yields status
``pending_policy``: the identity resolves, and the chokepoints still return
``403 no_governance_policy``. Enrollment can therefore never widen what an agent
may do, only make *who it is* resolvable.

What this module must never do
------------------------------
It never creates, modifies, or attaches a cloud identity. No code path here calls
``iam:CreateRole``, ``iam:PutRolePolicy``, or any equivalent, and the authority's
own execution role is expected to hold read-only identity permissions
(``iam:GetRole``, ``sts:GetCallerIdentity``). A service that can mint principals
can mint a privileged one for itself, which would dissolve the separation that
makes the authority authoritative. Principals are created out-of-band — by
Terraform, or by the developer's own SSO session — and this module verifies and
records them.

Enroller authorization
----------------------
The caller must be a human cloud principal. An IAM user or an AWS SSO
(``AWSReservedSSO_*``) session qualifies; an agent's own role does not, and a
principal that matches an existing agent binding is rejected outright so an agent
cannot enroll itself or a peer. CI needs an exception, so role names listed in
``GOV_ENROLL_ALLOWED_ROLES`` (comma-separated) are also accepted.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from galaxy_gov.remote.identity_store import IdentityBinding, IdentityStore, IdentityStoreError
from galaxy_gov.shared.policy_registry import policy_for

logger = logging.getLogger(__name__)

# Same shape the per-agent config enforces (payload_agents.config.AgentConfigModel).
_AGENT_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

STATUS_READY = "ready"
STATUS_PENDING_POLICY = "pending_policy"


class EnrollmentDenied(Exception):
    """The enrollment request was refused. The message is caller-facing."""


@dataclass(frozen=True)
class CallerIdentity:
    """The principal that authorized an enrollment request."""

    arn: str
    account: str
    user_id: str = ""

    @property
    def role_name(self) -> str:
        """The role name from an ``assumed-role`` ARN, else ``""``."""
        marker = ":assumed-role/"
        if marker not in self.arn:
            return ""
        return self.arn.split(marker, 1)[1].split("/", 1)[0]

    @property
    def is_iam_user(self) -> bool:
        return ":user/" in self.arn

    @property
    def is_sso_session(self) -> bool:
        return self.role_name.startswith("AWSReservedSSO_")


@dataclass(frozen=True)
class VerifiedPrincipal:
    """The result of verifying that a claimed principal actually exists."""

    principal_id: str
    account: str
    cloud: str = "aws"


@runtime_checkable
class PrincipalVerifier(Protocol):
    """Read-only identity verification, implemented per cloud under
    ``cloud_adapters/<cloud>/``. Implementations must use read-only APIs."""

    def caller_identity(self) -> CallerIdentity:
        """Who is making this request (AWS: ``sts:GetCallerIdentity``)."""
        ...

    def verify_principal(self, *, agent_type: str, principal_id: str) -> VerifiedPrincipal:
        """Confirm ``principal_id`` exists and is usable as an agent identity.
        Raises :class:`EnrollmentDenied` when it does not."""
        ...


@dataclass(frozen=True)
class EnrollmentResult:
    """Outcome of an enrollment, including the derived readiness status."""

    binding: IdentityBinding
    status: str
    rotated: bool
    message: str

    def to_dict(self) -> dict:
        return {
            "agent_type": self.binding.agent_type,
            "principal_id": self.binding.principal_id,
            "cloud": self.binding.cloud,
            "account": self.binding.account,
            "enrolled_at": self.binding.enrolled_at,
            "enrolled_by": self.binding.enrolled_by,
            "status": self.status,
            "rotated": self.rotated,
            "message": self.message,
        }


def _allowed_ci_roles() -> set[str]:
    return {r.strip() for r in (os.environ.get("GOV_ENROLL_ALLOWED_ROLES") or "").split(",") if r.strip()}


def authorize_enroller(caller: CallerIdentity, store: IdentityStore) -> None:
    """Reject any caller that is not a human (or explicitly allowed CI) principal.

    The decisive check is the first one: if the caller's principal is already
    bound to an agent type, the request is an agent attempting to enroll, which is
    exactly the self-registration escalation this design excludes."""
    try:
        bound = store.load()
    except IdentityStoreError as e:
        raise EnrollmentDenied(f"identity store unreadable, refusing to enroll: {e}") from e

    caller_role = caller.role_name
    for agent_type, binding in bound.items():
        principal_role = binding.principal_id.rsplit("/", 1)[-1]
        if binding.principal_id == caller.arn or (caller_role and caller_role == principal_role):
            raise EnrollmentDenied(
                f"caller {caller.arn} is the enrolled principal for agent type "
                f"{agent_type!r}; agents may not enroll identities"
            )

    if caller.is_iam_user or caller.is_sso_session:
        return
    if caller_role and caller_role in _allowed_ci_roles():
        logger.info("registrar.ci_enroller", extra={"role": caller_role})
        return
    raise EnrollmentDenied(
        f"caller {caller.arn} is not a human principal. Sign in with AWS SSO "
        f"(`aws sso login`) or add the role to GOV_ENROLL_ALLOWED_ROLES for CI."
    )


def enroll(
    agent_type: str,
    principal_id: str,
    *,
    verifier: PrincipalVerifier,
    store: Optional[IdentityStore] = None,
    registry: Optional[dict] = None,
    allow_rotate: bool = False,
) -> EnrollmentResult:
    """Verify and record an identity binding for ``agent_type``.

    Steps, in order, all of which must pass:
      1. ``agent_type`` is a well-formed PascalCase identifier.
      2. The caller is a human/CI principal and is not itself an enrolled agent.
      3. ``principal_id`` exists in the cloud (read-only verification).
      4. The binding is recorded, refusing a silent rebind unless ``allow_rotate``.

    The returned status is derived from the deployed policy registry, never
    stored, so it cannot go stale relative to what the chokepoints enforce.
    """
    if not agent_type or not _AGENT_TYPE_RE.match(agent_type):
        raise EnrollmentDenied(
            f"invalid agent type {agent_type!r}: expected PascalCase letters and digits")
    if not principal_id or not principal_id.strip():
        raise EnrollmentDenied("principal_id is required")

    store = store or IdentityStore()
    caller = verifier.caller_identity()
    authorize_enroller(caller, store)

    verified = verifier.verify_principal(agent_type=agent_type, principal_id=principal_id.strip())

    try:
        existing = store.load().get(agent_type)
    except IdentityStoreError as e:
        raise EnrollmentDenied(str(e)) from e
    rotated = bool(existing and existing.principal_id != verified.principal_id)

    binding = IdentityBinding(
        agent_type=agent_type,
        principal_id=verified.principal_id,
        cloud=verified.cloud,
        account=verified.account,
        enrolled_by=caller.arn,
    )
    try:
        recorded = store.put(binding, allow_rotate=allow_rotate)
    except IdentityStoreError as e:
        raise EnrollmentDenied(str(e)) from e

    has_policy = policy_for(registry or {}, agent_type) is not None
    status = STATUS_READY if has_policy else STATUS_PENDING_POLICY
    message = (
        f"{agent_type} is enrolled and has an approved control policy."
        if has_policy else
        f"{agent_type} is enrolled but has no approved control policy yet; the "
        f"chokepoints will continue to deny it with 403 no_governance_policy "
        f"until the governing team approves its governance block and the policy "
        f"registry is re-exported."
    )
    logger.info("registrar.enrolled", extra={
        "agent_type": agent_type, "principal_id": recorded.principal_id,
        "status": status, "rotated": rotated, "enrolled_by": caller.arn,
    })
    return EnrollmentResult(binding=recorded, status=status, rotated=rotated, message=message)


def resolve_identity(agent_type: str, *, store: Optional[IdentityStore] = None,
                     registry: Optional[dict] = None) -> Optional[dict]:
    """Read path for the ``GET /identity`` route and ``core.nhi_registry``.

    Returns ``None`` when no active binding exists (fail-closed). The response
    reports policy readiness so a caller can distinguish "unknown agent" from
    "enrolled but not yet approved" without being able to act on the difference.
    """
    store = store or IdentityStore()
    binding = store.get(agent_type)
    if binding is None:
        return None
    has_policy = policy_for(registry or {}, agent_type) is not None
    return {
        "agent_type": binding.agent_type,
        "principal_id": binding.principal_id,
        "cloud": binding.cloud,
        "account": binding.account,
        "status": STATUS_READY if has_policy else STATUS_PENDING_POLICY,
        "enrolled_at": binding.enrolled_at,
    }
