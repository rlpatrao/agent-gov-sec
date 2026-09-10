"""
galaxy_gov.remote.enforce — transport-neutral out-of-process enforcement.

The chokepoints (AWS API Gateway → Lambda today; AgentCore Gateway interceptors
in the AgentCore integration; a Fargate daemon off-AWS) all call these functions.
Each resolves the caller's policy from the NHI-keyed registry (fail-closed) and
re-verifies via the shared `EnforcementSession` — the *same* control logic the
in-process pipeline runs (trust-but-verify, one code path).

Nothing here parses a specific transport's payload; the per-deployment adapter
translates its event shape (Converse JSON, MCP interceptor event, HTTP body) into
these calls and back. That is why this module lives under `galaxy_gov/remote`
rather than in any cloud adapter.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from galaxy_gov.shared.enforcement.session import EnforcementSession, Verdict, build_enforcement
from galaxy_gov.shared.policy_registry import authorize_recipient, policy_for


def session_for(agent_type: Optional[str], registry: dict, *,
                nhi_id: Optional[str] = None, mediator: Any = None) -> Optional[EnforcementSession]:
    """Resolve a policy from the registry and build an EnforcementSession.
    Returns None when the identity is unknown — the caller must then deny."""
    policy = policy_for(registry, agent_type)
    if policy is None:
        return None
    return build_enforcement(
        policy, agent_id=agent_type or "remote", agent_type=agent_type,
        nhi_id=nhi_id, mediator=mediator,
    )


def enforce_input(session: EnforcementSession, text: str) -> Verdict:
    """Input-side guards over the concatenated request text."""
    return session.check_input(text)


def enforce_tool_plan(session: EnforcementSession,
                      tool_calls: Iterable[tuple[str, Any]]) -> Verdict:
    """Capability + blocked-pattern check over each tool the model intends to
    call. First block wins; an allowed plan returns a non-blocked Verdict."""
    for name, args in tool_calls:
        v = session.check_tool(name, args)
        if v.blocked:
            return v
    return Verdict()


def enforce_output(session: EnforcementSession, text: str) -> Verdict:
    """Output-side guards (PII/credential redaction, content-safety). The
    Verdict's `text` is the possibly-redacted output."""
    return session.check_output(text)


def authorize_a2a(sender_type: str, recipient: str, registry: dict) -> tuple[bool, str]:
    """A2A dispatch authorization from the sender's registry allow-list."""
    return authorize_recipient(sender_type, recipient, registry)
