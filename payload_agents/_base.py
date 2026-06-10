"""
agents/_base.py — single factory that builds a fully-instrumented MAF Agent.

Every Galaxy agent goes through `build_agent()`. Variations between agents
come from `agents/config/<name>.yaml`; the factory returns an AgentBundle
the caller owns (flush + close at end of run).

Design rules:
  - YAML is authoritative. Adding a new toggle means: extend
    `agents.config.GovernanceConfig` (or AgentConfigModel), then surface it
    here. Never read env vars for per-agent behavior — env is for runtime
    secrets and endpoint selection only.
  - Tools (MAF @tool callables) come in as Python callables. Their
    `__name__` is cross-checked against `governance.allowed_tools` so the
    YAML stays the source of truth for what an agent is permitted to do.
  - This file MUST stay agent-agnostic. No `if agent_name == "scanner"`.

The boilerplate this replaces lived inline at scanner_agent.py:228-302 and
ast_agent.py:177-243 — ~75 lines duplicated per agent. Phase 0.5 of the
agentrepo porting plan.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient
from agent_os.audit_logger import GovernanceAuditLogger

from payload_agents.config import AgentConfigModel, load_agent_config_cached
from core.interfaces import SecretProvider
from core.nhi_registry import NHIRegistry
from core.provider_factory import get_provider
from adapters.azure.audit import PostgresHashChainBackend
from adapters.azure.maf.middleware import build_governance_stack

logger = logging.getLogger(__name__)

_AGENTS_ROOT = Path(__file__).parent


@dataclass(frozen=True)
class AgentBundle:
    """Everything build_agent() produces, named.

    Caller owns lifecycle: at end of the run, call
        await bundle.pg_backend.flush_async()
        await bundle.pg_backend.verify_chain()
        bundle.audit_logger.flush()
        await bundle.pg_backend.close()
    """
    agent: Agent
    pg_backend: PostgresHashChainBackend
    audit_logger: GovernanceAuditLogger
    config: AgentConfigModel
    agent_id: str        # "<AgentType>-<nhi-client-id>"
    nhi_id: str
    egress: str          # gateway mode, e.g. "apim" | "aoai-direct"


async def build_agent(
    agent_name: str,
    run_id: str,
    *,
    prompt_file_override: Optional[str] = None,
    token_provider: Optional[SecretProvider] = None,
    tools: Optional[list[Callable[..., Any]]] = None,
) -> AgentBundle:
    """Build a fully-instrumented MAF Agent for `agent_name`.

    Reads `agents/config/<agent_name>.yaml` (normalized: hyphens → underscores,
    lowercased). Resolves the system prompt from the YAML's `prompt_file` and
    builds the governance stack with all toggles taken from the YAML.

    `prompt_file_override` swaps the main prompt file while keeping the YAML's
    shared_prompt_files (e.g. coder_rules.md).  Used by CoderHandler to load
    the per-stack prompt (coder_php_web_app.md, coder_java_spring_boot.md, …)
    without rebuilding the full governance stack per codebase_type.

    Tool agents (Coder, Tester, ...) pass `tools=[...]`; every callable's
    `__name__` must appear in `governance.allowed_tools` or this raises
    before constructing the agent. That keeps YAML authoritative.
    """
    cfg = load_agent_config_cached(agent_name)

    effective_prompt_file = prompt_file_override or cfg.prompt_file
    instructions = _load_prompt(agent_name, effective_prompt_file, cfg.shared_prompt_files)
    _validate_tool_allowlist(agent_name, cfg, tools)

    deployment = cfg.model or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-3-codex")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION") or "preview"

    identity = NHIRegistry.get(cfg.agent_type)
    agent_id = f"{cfg.agent_type}-{identity.client_id}"

    # The LLM gateway is the managed egress chokepoint, resolved per cloud
    # provider (Azure → APIM → Azure OpenAI, with direct-AOAI fallback). It
    # returns the endpoint, the key, and the attribution/auth headers
    # (x-agent-type, x-nhi-id, and Ocp-Apim-Subscription-Key in APIM mode).
    # x-galaxy-run-id / x-module-id are per-call, stamped via
    # `options.extra_headers` on agent.run() at the callsite.
    egress_resolution = get_provider().llm_gateway().resolve(
        agent_type=cfg.agent_type,
        client_id=identity.client_id,
        secret_provider=token_provider,
    )
    egress = egress_resolution.mode
    client = OpenAIChatClient(
        model=deployment,
        api_key=egress_resolution.api_key,
        azure_endpoint=egress_resolution.endpoint,
        api_version=api_version,
        default_headers=egress_resolution.default_headers,
    )

    middleware, pg_backend, audit = await build_governance_stack(
        agent_id=agent_id,
        run_id=run_id,
        allowed_tools=cfg.governance.allowed_tools or None,
        denied_tools=cfg.governance.denied_tools or None,
        enable_rogue_detection=cfg.governance.enable_rogue_detection,
        enable_prompt_injection_guard=cfg.governance.enable_prompt_injection_guard,
        enable_credential_redactor=cfg.governance.enable_credential_redactor,
        credential_mode=cfg.governance.credential_mode,
        enable_context_budget=cfg.governance.enable_context_budget,
        context_budget_total_tokens=cfg.governance.context_budget_tokens,
        prompt_injection_block_threshold=cfg.governance.prompt_injection_block_threshold,
    )

    agent_kwargs: dict[str, Any] = {
        "client": client,
        "instructions": instructions,
        "name": cfg.agent_type,
        "id": agent_id,
        "middleware": middleware,
    }
    if tools:
        agent_kwargs["tools"] = tools
    # Per-agent output-token cap, forwarded by MAF as default_options into
    # client.responses.create(...). The Responses API's parameter is exactly
    # `max_output_tokens` — see agent_framework_openai/_chat_client.py:1188
    # (`"max_tokens": "max_output_tokens"` remap).
    if cfg.max_output_tokens is not None:
        agent_kwargs["default_options"] = {"max_output_tokens": cfg.max_output_tokens}
    agent = Agent(**agent_kwargs)

    logger.info(
        "agent.built",
        extra={
            "run_id": run_id,
            "agent_name": agent_name,
            "agent_id": agent_id,
            "nhi_id": identity.client_id,
            "deployment": deployment,
            "egress": egress,
            "endpoint": endpoint,
            "tool_count": len(tools or []),
            "governance": {
                "prompt_injection":  cfg.governance.enable_prompt_injection_guard,
                "credential_guard":  cfg.governance.enable_credential_redactor,
                "credential_mode":   cfg.governance.credential_mode,
                "context_budget":    cfg.governance.enable_context_budget,
                "rogue_detection":   cfg.governance.enable_rogue_detection,
            },
        },
    )

    return AgentBundle(
        agent=agent,
        pg_backend=pg_backend,
        audit_logger=audit,
        config=cfg,
        agent_id=agent_id,
        nhi_id=identity.client_id,
        egress=egress,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_prompt(
    agent_name: str,
    prompt_file: str,
    shared_prompt_files: list[str] | None = None,
) -> str:
    """Resolve `prompt_file` (and optional shared fragments) relative to agents/ and read.

    Shared fragments are concatenated BEFORE the agent-specific prompt, in the
    order declared in YAML, separated by a blank line. Pattern: a single
    `prompts/_shared/quality-principles.md` injected into every agent so
    platform-wide rules ('produce JSON', 'never fabricate', 'log decisions')
    live in ONE place. Mirrors agentrepo/.../prompts/quality-principles.md.
    """
    parts: list[str] = []
    for shared in shared_prompt_files or []:
        shared_path = _AGENTS_ROOT / shared
        if not shared_path.exists():
            raise FileNotFoundError(
                f"shared_prompt_file not found for agent {agent_name!r}: {shared_path}."
            )
        shared_text = shared_path.read_text(encoding="utf-8").strip()
        if shared_text:
            parts.append(shared_text)

    prompt_path = _AGENTS_ROOT / prompt_file
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"prompt_file not found for agent {agent_name!r}: {prompt_path}. "
            f"Update agents/config/{agent_name}.yaml or add the missing file."
        )
    main_text = prompt_path.read_text(encoding="utf-8").strip()
    if not main_text:
        raise ValueError(f"prompt_file is empty for agent {agent_name!r}: {prompt_path}")
    parts.append(main_text)

    return "\n\n".join(parts)


def _validate_tool_allowlist(
    agent_name: str,
    cfg: AgentConfigModel,
    tools: Optional[list[Callable[..., Any]]],
) -> None:
    """Refuse to build an agent whose tool callables aren't in the YAML allow-list.

    This is the policy hand-shake between Python (callables) and YAML
    (allowed_tools). The CapabilityGuardMiddleware enforces at runtime;
    this check fails fast at construction so a typo in the YAML doesn't
    silently disable a tool.
    """
    if not tools:
        return
    # MAF's @tool decorator returns a FunctionTool (with `.name`), not a
    # plain function (`__name__`). Accept both — the FunctionTool path is
    # what real tool agents (Coder, Tester) use.
    tool_names = {getattr(fn, "name", None) or fn.__name__ for fn in tools}
    allowed = set(cfg.governance.allowed_tools)
    unknown = tool_names - allowed
    if unknown:
        raise ValueError(
            f"{agent_name}: tools {sorted(unknown)} are not declared in "
            f"governance.allowed_tools (declared: {sorted(allowed) or '[]'}). "
            f"Add them to agents/config/{agent_name}.yaml or remove from tools=."
        )


def extract_usage(response: Any) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from an AgentResponse.

    Returns (0, 0) when the framework doesn't expose usage — callers must
    treat zero as "unknown" rather than "free".
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    inp = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0)
    out = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", 0)
    return int(inp), int(out)


def extract_response_text(response: Any) -> str:
    """Pull the assistant text out of an AgentResponse, resilient to MAF shape drift.

    Shared by every agent that does its own parsing of the LLM reply.
    Previously duplicated at run_scanner.py:45 and ast_agent.py:161.
    """
    if hasattr(response, "text"):
        text = response.text
        if text:
            return text
    if hasattr(response, "messages"):
        for msg in response.messages:
            if hasattr(msg, "text") and msg.text:
                return msg.text
            if hasattr(msg, "content"):
                return str(msg.content)
    return str(response)
