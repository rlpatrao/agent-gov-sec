"""payload_agents.raw — the demo personas on the provider-native (no-framework) loop.

Each persona builds a governed agent via the raw _runner — a hand-rolled tool
loop that runs the agnostic-core GuardPipeline around each step, importing no
agent framework. Exposes the uniform framework surface (make_model + build_*).
Selected by --framework raw / GALAXY_FRAMEWORK.
"""

from __future__ import annotations

from payload_agents._lib.scripting import to_script_steps
from payload_agents.raw._runner import RawAgentBundle, ScriptedChatClient, build_agent
from payload_agents.raw.auditor import build_auditor_agent
from payload_agents.raw.finops import build_finops_agent
from payload_agents.raw.rogue import build_rogue_agent


def make_model(*messages):
    """Offline deterministic ChatModelClient (replays scripted turns as ScriptStep)."""
    return ScriptedChatClient(to_script_steps(messages))


__all__ = [
    "make_model", "build_finops_agent", "build_auditor_agent", "build_rogue_agent",
    "RawAgentBundle", "ScriptedChatClient", "build_agent",
]
