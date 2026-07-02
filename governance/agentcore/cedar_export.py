"""
governance.agentcore.cedar_export — generate AgentCore Policy (Cedar) from the registry.

AgentCore Policy enforces *coarse authorization* — which tool an agent (IAM
principal) may call at the Gateway — deterministically, outside agent code, in
Cedar. We do not hand-author Cedar; the NHI-keyed policy registry stays the
single source of truth and this module generates the statements from it. The
*content* controls Cedar cannot express (prompt-injection, PII/credential
redaction, FGAC masking, drift, reasoning) live in the governance interceptors.

The statement form is what AgentCore Policy actually accepts (verified live on an
AWS_IAM gateway):
    permit(
      principal == AgentCore::IamEntity::"arn:aws:sts::<acct>:assumed-role/<agent-role>",
      action == AgentCore::Action::"<targetName>___<tool>",
      resource == AgentCore::Gateway::"<gatewayArn>"
    );
A tool the agent's policy does not allow gets a `forbid` (forbid-wins). The
resource must be a real gateway ARN and the action a real `<target>___<tool>`,
so generation is parameterized by the deployed gateway/target.

`python -m governance.agentcore.cedar_export` prints the statements for the live
demo gateway (env-overridable).
"""

from __future__ import annotations

import os

from governance.policy_export import KNOWN_AGENT_TYPES, resolve_policy


def _statement(effect: str, principal_arn: str, action: str, gateway_arn: str) -> str:
    return (f'{effect}(principal == AgentCore::IamEntity::"{principal_arn}", '
            f'action == AgentCore::Action::"{action}", '
            f'resource == AgentCore::Gateway::"{gateway_arn}");')


def principal_arn(account_id: str, agent_type: str, role_prefix: str = "galaxy-rp-") -> str:
    """The Cedar IAM principal for an agent — its NHI assumed-role ARN."""
    return f'arn:aws:sts::{account_id}:assumed-role/{role_prefix}{agent_type.lower()}'


def iter_agentcore_policies(*, gateway_arn: str, target_name: str, gateway_tools,
                            account_id: str, role_prefix: str = "galaxy-rp-",
                            agent_types: tuple[str, ...] = KNOWN_AGENT_TYPES):
    """Yield (policy_name, cedar_statement) for every (agent, gateway tool): a
    permit when the agent's resolved policy allows the tool, else a forbid.
    Default-deny still applies; the explicit forbid documents the denial and wins
    over any permit."""
    prefix = f"{target_name}___"
    for at in agent_types:
        try:
            policy = resolve_policy(at)
        except Exception:
            continue
        principal = principal_arn(account_id, at, role_prefix)
        for tool in gateway_tools:
            effect = "permit" if tool in policy.allowed_tools else "forbid"
            yield f"{at.lower()}_{tool}", _statement(effect, principal, f"{prefix}{tool}", gateway_arn)


def export_cedar(**kwargs) -> str:
    """Render all generated statements as a Cedar document (for review/audit)."""
    header = ("// Generated from the governance policy registry (governance.policy_export).\n"
              "// Source of truth is the registry; edit the per-agent config, not this file.\n"
              "// Coarse authorization only — content controls live in the interceptors.\n")
    return header + "\n".join(stmt for _, stmt in iter_agentcore_policies(**kwargs)) + "\n"


if __name__ == "__main__":
    print(export_cedar(
        gateway_arn=os.environ.get("GW_ARN", "arn:aws:bedrock-agentcore:us-east-2:<ACCOUNT_ID>:gateway/galaxy-governance-gw"),
        target_name=os.environ.get("GW_TARGET", "galaxy-tools"),
        gateway_tools=os.environ.get("GW_TOOLS", "query_billing,summarize_costs,query_dataset").split(","),
        account_id=os.environ.get("GW_ACCOUNT", "<ACCOUNT_ID>"),
    ))
