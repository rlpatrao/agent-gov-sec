"""framework_adapters — the framework axis, counterpart of ``cloud_adapters``.

Each subpackage binds the framework-neutral governance surface (the
``GuardPipeline``, the floor-clamped agent config, the ``contract`` types) to one
agent framework:

    langgraph/   LangChain ``create_agent`` + ``GalaxyGuardMiddleware`` (plus the
                 gateway-backed and scripted chat models for that axis)
    pydantic/    Pydantic AI ``Agent`` via a governed model wrapper
    raw/         provider-native tool loop — the null adapter: same governance,
                 no framework import
    maf/         Microsoft Agent Framework middleware stack

This package itself imports no framework; each subpackage imports its own on
first use, gated by the matching pip extra (``[langgraph]``, ``[pydantic]``,
``[azure]`` for MAF). The two axes stay orthogonal: nothing here imports a cloud
SDK, and ``cloud_adapters`` imports nothing from here.

Demo persona builders (FinOps, Auditor, Rogue) remain in ``payload_agents/``,
which composes these adapters; agent teams in their own repositories compose
them the same way (see ``docs/shared/agentkit.md``).
"""
