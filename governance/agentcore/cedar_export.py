"""
governance.agentcore.cedar_export — generate AgentCore Policy (Cedar) from the registry.

AgentCore Policy enforces *coarse authorization* (which tool an agent may call,
which agent it may dispatch to) deterministically at the Gateway, outside agent
code, using the Cedar language. We do not hand-author Cedar; the NHI-keyed policy
registry stays the single source of truth and this module generates the Cedar
artifact from it. The *content* controls Cedar cannot express (prompt-injection,
PII/credential redaction, FGAC masking, drift, reasoning) remain in the
governance interceptors (see cloud_adapters/aws/agentcore).

Mapping (Cedar is default-deny, so a permit must match):
  * allowed_tools     → permit(... action == invokeTool ...) when resource.name in [...]
  * denied_tools      → forbid(... action == invokeTool ...) when resource.name in [...]
  * allowed_recipients→ permit(... action == dispatch  ...) when resource.agentType in [...]

`export_cedar()` returns the full policy set for deployment to an AgentCore policy
engine; `python -m governance.agentcore.cedar_export` prints it.
"""

from __future__ import annotations

from governance.policy_export import KNOWN_AGENT_TYPES, resolve_policy
from governance.shared.policy_registry import ControlPolicy


def _statements(policy: ControlPolicy) -> list[str]:
    """The agent's coarse-authz posture as individual, resource-scoped Cedar
    statements. AgentCore Policy rejects an unconstrained `resource` (wildcard),
    so each statement names one tool/recipient (`resource == Tool::"..."`).
    An empty allow-list emits nothing — Cedar default-denies."""
    at = policy.agent_type
    stmts: list[str] = []
    for t in policy.allowed_tools:
        stmts.append(f'permit(principal == Agent::"{at}", action == Action::"invokeTool", resource == Tool::"{t}");')
    for t in policy.denied_tools:
        stmts.append(f'forbid(principal == Agent::"{at}", action == Action::"invokeTool", resource == Tool::"{t}");')
    for r in policy.allowed_recipients:
        stmts.append(f'permit(principal == Agent::"{at}", action == Action::"dispatch", resource == Agent::"{r}");')
    return stmts


def policy_to_cedar(policy: ControlPolicy) -> str:
    """Render one agent's coarse-authz posture as a commented Cedar block."""
    return "\n".join([f"// ── {policy.agent_type} ──", *_statements(policy)])


def iter_cedar_statements(agent_types: tuple[str, ...] = KNOWN_AGENT_TYPES):
    """Yield (policy_name, single_cedar_statement) pairs, one per permit/forbid,
    for deployment to an AgentCore policy engine (one statement per create-policy)."""
    for at in agent_types:
        try:
            policy = resolve_policy(at)
        except Exception:
            continue
        for i, stmt in enumerate(_statements(policy)):
            yield f"{at.lower()}_{i}", stmt


def export_cedar(agent_types: tuple[str, ...] = KNOWN_AGENT_TYPES) -> str:
    """Generate the full Cedar policy set for the known agents."""
    header = (
        "// Generated from the governance policy registry (governance.policy_export).\n"
        "// Source of truth is the registry; edit the per-agent config, not this file.\n"
        "// Coarse authorization only — content controls live in the interceptors.\n"
    )
    blocks = []
    for at in agent_types:
        try:
            blocks.append(policy_to_cedar(resolve_policy(at)))
        except Exception:
            continue
    return header + "\n\n".join(blocks) + "\n"


if __name__ == "__main__":
    print(export_cedar())
