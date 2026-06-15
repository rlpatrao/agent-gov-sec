"""
agent_framework_adapters.langgraph._base — the LangGraph agent factory.

``build_langgraph_agent`` is the LangGraph counterpart to
``payload_agents/_base.build_agent`` (the MAF factory). It wires the same
cross-cutting governance posture around a LangGraph ``create_agent``:

  - **Identity (A1):** resolves the agent's NHI via ``core.nhi_registry``.
  - **Egress chokepoint (A2):** consults the cloud provider's LLM gateway
    (``core.provider_factory``). Offline (no API key) the gateway *refuses* to
    hand back an egress credential — the chokepoint working as designed — and
    the factory falls back to the supplied offline model. The agent never holds
    a raw provider key.
  - **Governance (B–G, H21/H22):** builds the ``GalaxyGuardMiddleware`` stack
    (``build_langgraph_governance``) from the per-agent YAML config.
  - **Audit ledger (H21):** the hash-chained ``PostgresHashChainBackend`` is
    returned in the bundle for end-of-run flush + chain verification.

YAML is authoritative: every governance toggle, the tool allow-list, and the
A2A recipient list come from ``payload_agents/config/<name>.yaml`` via the
existing ``AgentConfigModel`` loader — no per-agent branching here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel

from agent_framework_adapters.contract import RunResult, ToolCall, Turn
from agent_framework_adapters.langgraph.governance import build_langgraph_governance
from agent_os.audit_logger import GovernanceAuditLogger
from core.interfaces import SecretProvider
from core.nhi_registry import NHIRegistry
from governance.extensions.data_classification import DataClassificationCatalog
from governance.extensions.data_fgac import DataAccessMediator
from payload_agents.config import AgentConfigModel, load_agent_config_cached

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LangGraphAgentBundle:
    """Everything ``build_langgraph_agent`` produces. The caller owns lifecycle:
    at end of run, ``await pg_backend.flush_async()`` / ``verify_chain()`` /
    ``close()``."""
    agent: Any                       # the compiled LangGraph (create_agent return)
    pg_backend: Any                  # the provider's hash-chain AuditBackend (azure/aws/gcp/local)
    audit_logger: GovernanceAuditLogger
    mediator: Optional[DataAccessMediator]
    config: AgentConfigModel
    agent_id: str
    nhi_id: str
    egress: str

    def invoke(self, prompt: str) -> RunResult:
        """Run the agent on ``prompt`` and return a framework-neutral ``RunResult``.
        A blocking guard raises ``GovernanceViolation`` (propagated) exactly as in
        the raw/pydantic adapters — the demo handles it uniformly."""
        raw = self.agent.invoke({"messages": [{"role": "user", "content": prompt}]})
        return _normalize(raw)


def _normalize(raw: Any) -> RunResult:
    """Convert a LangGraph result dict (``{"messages": [...]}``) into a RunResult."""
    turns: list[Turn] = []
    for m in (raw.get("messages", []) if isinstance(raw, dict) else []):
        cls = m.__class__.__name__
        if cls == "AIMessage":
            tool_calls = [
                ToolCall(name=tc.get("name", ""), args=tc.get("args", {}) or {}, id=tc.get("id", ""))
                for tc in (getattr(m, "tool_calls", None) or [])
            ]
            turns.append(Turn(role="ai", text=_flatten(getattr(m, "content", "")), tool_calls=tool_calls))
        elif cls == "ToolMessage":
            turns.append(Turn(role="tool", text=str(m.content), tool_name=getattr(m, "name", "") or ""))
    return RunResult(turns=turns)


def _flatten(content: Any) -> str:
    """LangChain message content (str | list of typed parts) → human-facing text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def _resolve_egress(agent_type: str, client_id: str, token_provider: Optional[SecretProvider]) -> str:
    """Consult the LLM-gateway chokepoint. Returns the resolved mode, or
    ``"offline-no-egress"`` when no credential is available (the chokepoint
    correctly refusing to leak a key for an offline run)."""
    try:
        from core.provider_factory import get_provider
        resolution = get_provider().llm_gateway().resolve(
            agent_type=agent_type, client_id=client_id, secret_provider=token_provider,
        )
        return resolution.mode
    except Exception as e:
        logger.info("langgraph.egress.offline", extra={"agent_type": agent_type, "reason": str(e)[:80]})
        return "offline-no-egress"


async def build_langgraph_agent(
    agent_name: str,
    run_id: str,
    *,
    model: BaseChatModel,
    tools: Optional[list[Callable[..., Any]]] = None,
    token_provider: Optional[SecretProvider] = None,
    catalog: Optional[DataClassificationCatalog] = None,
    mediator: Optional[DataAccessMediator] = None,
) -> LangGraphAgentBundle:
    """Build a fully-governed LangGraph agent for ``agent_name``.

    ``model`` is injected (the offline demo/tests pass a ``FakeToolCallingModel``;
    live runs pass an ``AzureChatOpenAI``/``ChatOpenAI`` from
    ``runtime.build_chat_model``). ``tools`` are LangChain ``@tool`` callables;
    their names should appear in the YAML ``governance.allowed_tools`` (the
    reasoning-step guard enforces this at runtime — an unlisted tool is denied).
    """
    cfg = load_agent_config_cached(agent_name)

    identity = NHIRegistry.get(cfg.agent_type)        # A1 — raises for an unregistered type
    agent_id = f"{cfg.agent_type}-{identity.client_id}"
    egress = _resolve_egress(cfg.agent_type, identity.client_id, token_provider)   # A2

    g = cfg.governance
    middleware, pg_backend, audit, mediator = await build_langgraph_governance(
        agent_id=agent_id,
        agent_type=cfg.agent_type,
        nhi_id=identity.client_id,
        run_id=run_id,
        allowed_tools=g.allowed_tools or None,
        blocked_patterns=getattr(g, "blocked_patterns", None) or ["DROP TABLE", "rm -rf"],
        prompt_injection_block_threshold=g.prompt_injection_block_threshold,
        enable_prompt_injection_guard=g.enable_prompt_injection_guard,
        enable_credential_redactor=g.enable_credential_redactor,
        credential_mode=g.credential_mode,
        enable_context_budget=g.enable_context_budget,
        context_budget_tokens=g.context_budget_tokens,
        enable_data_fgac=getattr(g, "enable_data_fgac", False),
        enable_data_drift=getattr(g, "enable_data_drift", False),
        enable_reasoning_guard=getattr(g, "enable_reasoning_guard", False),
        enable_reasoning_trace=getattr(g, "enable_reasoning_trace", False),
        catalog=catalog,
        mediator=mediator,
    )

    agent = create_agent(model=model, tools=list(tools or []), middleware=middleware)

    logger.info("langgraph.agent.built", extra={
        "run_id": run_id, "agent_name": agent_name, "agent_id": agent_id,
        "nhi_id": identity.client_id, "egress": egress, "tool_count": len(tools or []),
        "governance": {"fgac": getattr(g, "enable_data_fgac", False),
                       "drift": getattr(g, "enable_data_drift", False),
                       "reasoning_guard": getattr(g, "enable_reasoning_guard", False)},
    })

    return LangGraphAgentBundle(
        agent=agent, pg_backend=pg_backend, audit_logger=audit, mediator=mediator,
        config=cfg, agent_id=agent_id, nhi_id=identity.client_id, egress=egress,
    )
