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


def _quote_list(values) -> str:
    return ", ".join(f'"{v}"' for v in values)


def policy_to_cedar(policy: ControlPolicy) -> str:
    """Render one agent's coarse-authz posture as Cedar statements."""
    at = policy.agent_type
    out: list[str] = [f"// ── {at} ──"]

    if policy.allowed_tools:
        out.append(
            f'permit(\n'
            f'  principal == Agent::"{at}",\n'
            f'  action == Action::"invokeTool",\n'
            f'  resource\n'
            f') when {{ resource.name in [{_quote_list(policy.allowed_tools)}] }};'
        )
    else:
        # No tools permitted → an explicit forbid documents the deny-all intent
        # (Cedar would deny by default, but this is clearer for auditors).
        out.append(
            f'forbid(\n'
            f'  principal == Agent::"{at}",\n'
            f'  action == Action::"invokeTool",\n'
            f'  resource\n'
            f');'
        )

    if policy.denied_tools:
        out.append(
            f'forbid(\n'
            f'  principal == Agent::"{at}",\n'
            f'  action == Action::"invokeTool",\n'
            f'  resource\n'
            f') when {{ resource.name in [{_quote_list(policy.denied_tools)}] }};'
        )

    if policy.allowed_recipients:
        out.append(
            f'permit(\n'
            f'  principal == Agent::"{at}",\n'
            f'  action == Action::"dispatch",\n'
            f'  resource\n'
            f') when {{ resource.agentType in [{_quote_list(policy.allowed_recipients)}] }};'
        )

    return "\n".join(out)


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
