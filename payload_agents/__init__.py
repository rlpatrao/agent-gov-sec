"""
payload_agents — the demonstration payload (governed by the platform).

Registers the demo agents' Non-Human Identities **in-zone**, via env defaults,
so ``core/nhi_registry`` carries no payload-specific agent names. The registry's
``NHI_CLIENT_ID_<AGENT_TYPE>`` env fallback (see ``core.nhi_registry``) then
resolves them. A real tenant overrides these with its own Entra App / IAM role /
GCP SA ids by setting the same env vars before import.

The agent list is **derived** from ``payload_agents/config/*.yaml`` rather than
hardcoded, so an agent added with ``galaxy new-agent`` gets its local-dev default
automatically. Previously this module held its own copy of the agent names, which
meant a newly scaffolded agent had no default and the offline demo failed for it.

These defaults are a local-development convenience only. They are ignored in any
deployment that configures ``GOV_AUTHORITY_ENDPOINT``, where the identity binding
is resolved from the Governance Authority instead — an agent must not be able to
supply its own identity.
"""

import os as _os


def _register_local_nhi_defaults() -> None:
    """`setdefault` a placeholder NHI for every discovered agent type, so the
    offline demo runs with no env configuration. A real ``NHI_CLIENT_ID_*`` in the
    environment always wins."""
    try:
        from governance.policy_export import discover_agent_types
        agent_types = discover_agent_types()
    except Exception:
        # Never let identity convenience break importing the package.
        return
    for agent_type in agent_types:
        _os.environ.setdefault(
            f"NHI_CLIENT_ID_{agent_type.upper()}", f"local-{agent_type.lower()}-nhi")


_register_local_nhi_defaults()
