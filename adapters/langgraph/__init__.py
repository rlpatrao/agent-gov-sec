"""
adapters.langgraph — the LangGraph / LangChain framework axis.

This package is the **framework binding** for governing LangGraph agents, the
non-MAF counterpart to ``adapters/azure/maf/``. It proves the platform's
framework-agnostic claim: the same cloud-neutral governance primitives
(``governance/`` + the ``agent_os`` detectors) and the same WS7 extensions wrap a
LangGraph ``create_agent`` through a thin LangChain ``AgentMiddleware`` shim
(``GalaxyGuardMiddleware``), exactly as they wrap a MAF ``Agent`` through the MAF
guard middlewares.

Cloud selection stays orthogonal: identity / secrets / tracing / audit / egress /
LLM-gateway are resolved via ``core.provider_factory`` (azure | aws | gcp), so a
LangGraph agent governed here is both framework- and cloud-agnostic.

Public surface:
  - ``build_langgraph_agent``  — the agent factory (mirrors payload_agents/_base.build_agent)
  - ``build_langgraph_governance`` — the middleware-stack assembly
  - ``GalaxyGuardMiddleware`` — the governance AgentMiddleware
  - ``GovernanceViolation`` — raised when a guard blocks a call
  - ``FakeToolCallingModel`` / ``scripted_model`` — offline (no-creds) chat model
"""

from __future__ import annotations

from adapters.langgraph.governance import (
    GalaxyGuardMiddleware,
    GovernanceViolation,
    build_langgraph_governance,
)
from adapters.langgraph.runtime import FakeToolCallingModel, build_chat_model, scripted_model

__all__ = [
    "build_langgraph_governance",
    "GalaxyGuardMiddleware",
    "GovernanceViolation",
    "FakeToolCallingModel",
    "scripted_model",
    "build_chat_model",
]
