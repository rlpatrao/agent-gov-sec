"""framework_adapters.pydantic — governance binding for Pydantic AI.

``GovernedModel`` wraps any Pydantic AI model and runs the agnostic-core
``GuardPipeline`` around each request. Requires the ``[pydantic]`` extra.
"""

from framework_adapters.pydantic.runner import GovernedModel, PydanticAgentBundle, build_agent

__all__ = ["GovernedModel", "PydanticAgentBundle", "build_agent"]
