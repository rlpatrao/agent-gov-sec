"""
governance.shared.policy_registry — the NHI-keyed control authority (consumer half).

This module is dependency-free (stdlib only) so it can be vendored into a Lambda
or interceptor without pulling the agent codebase. It defines the resolved policy
shape (:class:`ControlPolicy`), the fail-closed lookups (:func:`load_registry`,
:func:`policy_for`, :func:`authorize_recipient`), and the explicit deny posture
(:data:`DENY_ALL`).

The *producer* side — building the registry from the per-agent config (which
imports ``payload_agents``) — lives in ``governance.policy_export``, kept separate
precisely so this consumer half carries no agent-codebase dependency.

Fail-closed: :func:`policy_for` returns ``None`` for an unknown identity; a
chokepoint that cannot resolve a policy must deny the request.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ControlPolicy:
    """The full resolved control posture for one agent type. Consumed by all
    enforcement tiers; the ``model_boundary`` slice is consumed by
    ``shared.enforcement.session.build_enforcement``."""

    agent_type: str
    model_boundary: dict = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    allowed_recipients: tuple[str, ...] = ()
    a2a_timeout_seconds: int = 30
    a2a_max_files: int = 0
    # Data-layer gates (enforced by the data-access proxy, which owns the
    # classification catalog; here we only carry whether the gates are on).
    data_fgac: bool = False
    data_drift: bool = False
    reasoning_guard: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# Explicit deny-all posture for fail-closed callers.
DENY_ALL = ControlPolicy(
    agent_type="<unknown>",
    model_boundary={
        "injection_enabled": True, "injection_threshold": "medium",
        "credential_enabled": True, "credential_mode": "deny",
        "budget_enabled": True, "budget_max_tokens": 1,
        "output_pii_enabled": True, "blocked_patterns": [],
    },
    allowed_tools=(), denied_tools=(), allowed_recipients=(),
    data_fgac=True, data_drift=True, reasoning_guard=True,
)


def load_registry(raw: str | dict) -> dict:
    """Parse a serialized registry (JSON string or dict). Pure stdlib."""
    return raw if isinstance(raw, dict) else json.loads(raw)


def policy_for(registry: dict, identity: Optional[str]) -> Optional[dict]:
    """Resolve the policy dict for an identity (agent type). Fail-closed:
    returns None for a missing/unknown identity, so the caller denies."""
    if not identity or not registry:
        return None
    return (registry.get("agents") or {}).get(identity)


def authorize_recipient(sender_type: str, recipient: str, registry: dict) -> tuple[bool, str]:
    """Decide whether ``sender_type`` may dispatch an A2A call to ``recipient``,
    using the sender's allow-list in the serialized ``registry``. The recipient
    may be a bare type ('Auditor') or an NHI-qualified id ('Auditor-abc'); the
    type is the first '-'-delimited segment. Fail-closed: an unknown sender is
    denied. (In-process callers that don't hold a registry should use
    ``governance.policy_export.authorize_recipient_live``.)"""
    sender_policy = policy_for(registry, sender_type)
    allowed = list((sender_policy or {}).get("allowed_recipients") or []) if sender_policy else None
    if allowed is None:
        return False, f"sender {sender_type!r} has no governance policy"
    recipient_type = (recipient or "").split("-", 1)[0]
    if recipient_type not in allowed:
        return False, f"{sender_type} may not dispatch to {recipient_type} (allowed: {allowed})"
    return True, "ok"
