"""
agent_framework_adapters — per-framework agent bindings behind the shared GuardPipeline.

Each sub-package is a thin shim that maps a framework's hooks onto the framework-neutral
``GuardPipeline`` (governance), so the same governance runs under every framework:

  agent_framework_adapters/contract.py     the neutral contract every binding implements
                                           (ToolSpec / ToolCall / Turn / RunResult /
                                           AgentBundle / ChatModelClient)
  agent_framework_adapters/langgraph/      LangChain create_agent + GalaxyGuardMiddleware
  agent_framework_adapters/raw/            provider-native tool loop, no agent framework
  agent_framework_adapters/pydantic_ai/    Pydantic AI Agent over native models

Selected at runtime by ``core.framework_factory.get_framework()`` (keyed on
``GALAXY_FRAMEWORK`` / the ``--framework`` flag). This axis is orthogonal to the
cloud axis (``cloud_adapters``): any framework composes with any cloud.
"""
