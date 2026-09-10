"""framework_adapters.raw — the provider-native tool loop (no framework import).

The null adapter: a hand-rolled plan -> tool -> observe loop that runs the same
agnostic-core ``GuardPipeline`` around each step. Demonstrates that the
governance does not depend on any agent framework.
"""

from framework_adapters.raw.runner import RawAgentBundle, ScriptedChatClient, build_agent

__all__ = ["RawAgentBundle", "ScriptedChatClient", "build_agent"]
