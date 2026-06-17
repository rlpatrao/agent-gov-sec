"""
governance.policy_registry — the single, NHI-keyed control authority.

Every chokepoint (LLM proxy, data-access proxy, A2A broker) and the in-process
pipeline resolve an agent's control posture from here, rather than each trusting
the agent's own YAML at request time. This is the unifying piece of the
full-out-of-process model (mechanism 4 + the previously-deferred mechanism 3):
one authoritative, governing-team-owned policy, two enforcement tiers
(out-of-process and in-process), fail-closed by default.

Two access shapes:

  * In-process: :func:`resolve_policy` builds a :class:`ControlPolicy` from the
    per-agent config (which has already passed the non-overridable floor) plus
    the A2A limits. The floor guarantees the resolved posture is never weaker
    than the baseline.
  * Out-of-process: :func:`export_registry` serialises every known agent's
    resolved policy to a plain dict (JSON-dumpable). A chokepoint running in a
    Lambda bundles that JSON and loads it with :func:`load_registry` /
    :func:`policy_for` — dependency-free, no import of the agent codebase.

Fail-closed: :func:`policy_for` returns ``None`` for an unknown identity, and
:data:`DENY_ALL` is provided for callers that prefer an explicit deny posture.
A chokepoint that cannot resolve a policy must deny the request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Agent types the platform knows about. The filesystem (payload_agents/config/
# *.yaml) is the source of truth; this list drives export_registry().
KNOWN_AGENT_TYPES = ("FinOps", "Auditor", "Rogue")


@dataclass(frozen=True)
class ControlPolicy:
    """The full resolved control posture for one agent type. Consumed by all
    enforcement tiers; the ``model_boundary`` slice feeds
    ``enforcement_core.ModelBoundaryPosture.from_dict``."""

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


def resolve_policy(agent_type: str) -> ControlPolicy:
    """Build the resolved (floored) control posture for ``agent_type`` from its
    per-agent config. Raises if the agent has no config — unknown agents have no
    posture and must be denied, never defaulted to permissive."""
    from payload_agents.config import load_agent_config  # local: avoid import cycle

    cfg = load_agent_config(agent_type)          # floor already applied inside
    g = cfg.governance
    model_boundary = {
        "injection_enabled": g.enable_prompt_injection_guard,
        "injection_threshold": g.prompt_injection_block_threshold,
        "credential_enabled": g.enable_credential_redactor,
        "credential_mode": g.credential_mode,
        "budget_enabled": g.enable_context_budget,
        "budget_max_tokens": g.context_budget_tokens,
        "output_pii_enabled": True,   # output redaction is always-on at the boundary
        "blocked_patterns": list(g.blocked_patterns),
    }
    return ControlPolicy(
        agent_type=cfg.agent_type,
        model_boundary=model_boundary,
        allowed_tools=tuple(g.allowed_tools),
        denied_tools=tuple(g.denied_tools),
        allowed_recipients=tuple(cfg.a2a.allowed_recipients),
        a2a_timeout_seconds=cfg.a2a.timeout_seconds,
        a2a_max_files=cfg.a2a.max_files_per_dispatch,
        data_fgac=g.enable_data_fgac,
        data_drift=g.enable_data_drift,
        reasoning_guard=g.enable_reasoning_guard,
    )


def export_registry(agent_types: tuple[str, ...] = KNOWN_AGENT_TYPES) -> dict:
    """Resolve every known agent and return a JSON-dumpable registry keyed by
    agent type. This is the artifact deployed to each out-of-process chokepoint."""
    registry: dict = {"version": "1.0", "default": "deny", "agents": {}}
    for at in agent_types:
        try:
            registry["agents"][at] = resolve_policy(at).to_dict()
        except Exception as e:
            logger.warning("policy_registry.resolve_failed", extra={"agent": at, "error": str(e)})
    return registry


def export_registry_json(agent_types: tuple[str, ...] = KNOWN_AGENT_TYPES) -> str:
    return json.dumps(export_registry(agent_types), indent=2, sort_keys=True)


# ── Dependency-free consumer side (used by the chokepoints) ──────────────────


def load_registry(raw: str | dict) -> dict:
    """Parse a serialized registry (JSON string or dict). Pure stdlib."""
    return raw if isinstance(raw, dict) else json.loads(raw)


def policy_for(registry: dict, identity: Optional[str]) -> Optional[dict]:
    """Resolve the policy dict for an identity (agent type). Fail-closed:
    returns None for a missing/unknown identity, so the caller denies."""
    if not identity or not registry:
        return None
    return (registry.get("agents") or {}).get(identity)


def authorize_recipient(sender_type: str, recipient: str,
                        registry: Optional[dict] = None) -> tuple[bool, str]:
    """Decide whether ``sender_type`` may dispatch an A2A call to ``recipient``.

    The recipient may be a bare type ('Auditor') or an NHI-qualified id
    ('Auditor-abc'); the type is the first '-'-delimited segment. The allow-list
    comes from the sender's resolved policy — out-of-process callers pass a
    serialized ``registry``; in-process callers omit it and the sender policy is
    resolved live. Fail-closed: an unknown sender is denied."""
    if registry is not None:
        sender_policy = policy_for(registry, sender_type)
        allowed = list((sender_policy or {}).get("allowed_recipients") or []) if sender_policy else None
    else:
        try:
            allowed = list(resolve_policy(sender_type).allowed_recipients)
        except Exception:
            allowed = None
    if allowed is None:
        return False, f"sender {sender_type!r} has no governance policy"
    recipient_type = (recipient or "").split("-", 1)[0]
    if recipient_type not in allowed:
        return False, f"{sender_type} may not dispatch to {recipient_type} (allowed: {allowed})"
    return True, "ok"
