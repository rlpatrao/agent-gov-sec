"""framework_adapters.langgraph — governance binding for LangChain / LangGraph.

``GalaxyGuardMiddleware`` runs the agnostic-core ``GuardPipeline`` as a LangChain
``AgentMiddleware``; ``build_langgraph_agent`` assembles a governed
``create_agent`` from a floor-clamped config. Requires the ``[langgraph]`` extra.
"""

from framework_adapters.langgraph.guard import GalaxyGuardMiddleware, build_langgraph_governance
from framework_adapters.langgraph.runner import LangGraphAgentBundle, build_langgraph_agent
from framework_adapters.langgraph.models import (
    FakeToolCallingModel,
    build_bedrock_model,
    build_chat_model,
    build_gemini_model,
    scripted_model,
)

__all__ = [
    "GalaxyGuardMiddleware",
    "build_langgraph_governance",
    "LangGraphAgentBundle",
    "build_langgraph_agent",
    "FakeToolCallingModel",
    "build_bedrock_model",
    "build_chat_model",
    "build_gemini_model",
    "scripted_model",
]
