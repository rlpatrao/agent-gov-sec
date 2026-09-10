"""
cloud_adapters/aws/agentcore/identity.py — map NHI onto AgentCore Identity.

AgentCore Identity is a managed agent identity/auth service (OAuth, token vault,
IdP federation with Cognito/Okta/Entra/Auth0). This adapter implements the
platform's `IdentityProvider` protocol against it, so the rest of the framework
keeps using the portable `NHIRegistry` abstraction while, on AWS, the principal
and credentials come from AgentCore Identity.

Resolution order for `resolve_client_id`:
  1. AgentCore Identity workload identity for the agent type (when configured via
     AGENTCORE_IDENTITY_<AGENT_TYPE> / the AgentCore SDK).
  2. The portable `NHI_CLIENT_ID_<AGENT_TYPE>` env bridge (IaC-populated).
`get_credential` exchanges the principal for an OAuth token via AgentCore
Identity's token vault; returns None offline. The AgentCore SDK calls are kept
behind lazy imports so the adapter imports cleanly without the SDK present.
"""

from __future__ import annotations

import os
from typing import Any, Optional


class AgentCoreIdentityProvider:
    """`IdentityProvider` backed by AgentCore Identity, with the portable env
    bridge as the offline/fallback path."""

    def resolve_client_id(self, *, agent_type: str) -> Optional[str]:
        # 1) AgentCore Identity (workload identity / OAuth client for the agent).
        configured = os.environ.get(f"AGENTCORE_IDENTITY_{agent_type.upper()}")
        if configured:
            return configured
        # 2) Portable NHI env bridge (same convention as core.nhi_registry).
        return os.environ.get(f"NHI_CLIENT_ID_{agent_type.upper()}") or None

    def get_credential(self, *, client_id: str, agent_type: str) -> Optional[Any]:
        """Exchange the principal for an AgentCore Identity-issued token. Lazy
        import keeps the adapter usable without the AgentCore SDK; returns None
        when the SDK/identity is unavailable (e.g. local dev)."""
        try:
            from bedrock_agentcore.identity import WorkloadIdentity  # type: ignore
        except Exception:
            return None
        try:
            return WorkloadIdentity(client_id=client_id).get_token(scope=agent_type)
        except Exception:
            return None
