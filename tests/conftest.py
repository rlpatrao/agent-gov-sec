"""Test-suite wiring for the in-tree demo payload.

The platform names no application paths: the agent-config directory and the
data-proxy row source are injected by the application (payload_agents registers
its config directory on import; a deployment sets the variables in its
environment). The test suite exercises the platform *through* the demo payload,
so it declares the demo's wiring here — the same two variables a real
deployment would set.

setdefault, so a test that configures its own environment still wins.
"""

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

os.environ.setdefault("GALAXY_AGENT_CONFIG_DIR", str(_REPO / "payload_agents" / "config"))
os.environ.setdefault("GOV_DATA_SOURCE_MODULE", "payload_agents._lib.demo_data")
