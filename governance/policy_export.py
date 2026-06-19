"""
governance.policy_export — build the policy registry from per-agent config.

The *producer* side of the policy registry (the consumer side is
``governance.shared.policy_registry``). This module imports ``payload_agents``
to resolve each agent's floored posture, so it is build-time / in-process only —
it is deliberately NOT under ``governance/shared`` so the consumer half stays
free of any agent-codebase dependency and remains vendorable into a Lambda.

`export_registry_json()` produces the artifact deployed to the out-of-process
chokepoints; `resolve_policy()` is used in-process.
"""

from __future__ import annotations

import json
import logging

from governance.shared.policy_registry import ControlPolicy, authorize_recipient

logger = logging.getLogger(__name__)

# Agent types the platform knows about. The filesystem (payload_agents/config/
# *.yaml) is the source of truth; this list drives export_registry().
KNOWN_AGENT_TYPES = ("FinOps", "Auditor", "Rogue")


def resolve_policy(agent_type: str) -> ControlPolicy:
    """Build the resolved (floored) control posture for ``agent_type`` from its
    per-agent config. Raises if the agent has no config — unknown agents have no
    posture and must be denied, never defaulted to permissive."""
    from payload_agents.config import load_agent_config

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
            logger.warning("policy_export.resolve_failed", extra={"agent": at, "error": str(e)})
    return registry


def export_registry_json(agent_types: tuple[str, ...] = KNOWN_AGENT_TYPES) -> str:
    return json.dumps(export_registry(agent_types), indent=2, sort_keys=True)


def authorize_recipient_live(sender_type: str, recipient: str) -> tuple[bool, str]:
    """In-process A2A authorization: resolve the sender's policy live and apply
    the dependency-free decision. For callers that don't hold a serialized
    registry (e.g. the in-process dispatcher)."""
    try:
        registry = {"agents": {sender_type: resolve_policy(sender_type).to_dict()}}
    except Exception:
        return False, f"sender {sender_type!r} has no governance policy"
    return authorize_recipient(sender_type, recipient, registry)


if __name__ == "__main__":
    # `python -m governance.policy_export` prints the registry artifact to stdout,
    # used at deploy time to bake the chokepoint policy (agent-controls.json).
    print(export_registry_json())
