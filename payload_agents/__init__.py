"""
payload_agents — the demonstration payload (governed by the platform).

Registers the demo agents' Non-Human Identities **in-zone**, via env defaults,
so ``core/nhi_registry`` carries no payload-specific agent names. The registry's
``NHI_CLIENT_ID_<AGENT_TYPE>`` env fallback (see ``core.nhi_registry``) then
resolves them. A real tenant overrides these with its own Entra App / IAM role /
GCP SA ids by setting the same env vars before import.
"""

import os as _os

# Demo NHIs — non-empty local-dev defaults so the offline demo runs with no env
# config. `setdefault` means a real NHI_CLIENT_ID_* in the environment wins.
for _agent, _local_default in (
    ("FINOPS", "local-finops-nhi"),
    ("AUDITOR", "local-auditor-nhi"),
    ("ROGUE", "local-rogue-nhi"),
):
    _os.environ.setdefault(f"NHI_CLIENT_ID_{_agent}", _local_default)
