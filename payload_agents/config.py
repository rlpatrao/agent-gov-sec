"""
agents/config.py

Load and validate per-agent configuration from YAML using Pydantic v2.

The YAML files in `agents/config/*.yaml` are the authoritative source of
per-agent tunables — nothing here should duplicate them as Python defaults.
If a required field is missing from the YAML, Pydantic raises — loud is good.

Usage:
    from payload_agents.config import load_agent_config_cached

    config = load_agent_config_cached("scanner")
    config.agent_type                         # "Scanner"
    config.a2a.allowed_recipients             # ["ASTAnalyzer"]
    config.a2a.max_files_per_dispatch         # 40
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


# ── Pydantic schema ──────────────────────────────────────────────────────────

class A2AConfig(BaseModel):
    """Agent-to-Agent envelope limits and routing allow-list."""
    model_config = ConfigDict(extra="forbid")

    allowed_recipients: list[str] = Field(
        description="Agent types this agent may dispatch A2A calls to. "
                    "Empty list = leaf agent (doesn't dispatch).",
    )
    max_files_per_dispatch: int = Field(
        ge=0, le=10000,
        description="Cap on the number of files in a single A2A envelope. "
                    "Zero is valid for leaf agents that never dispatch.",
    )
    timeout_seconds: int = Field(
        gt=0, le=3600,
        description="Per-call A2A deadline.",
    )


class GovernanceConfig(BaseModel):
    """Toggles for the per-agent governance middleware stack.

    All fields default to safe values so existing YAMLs that only set
    `enable_rogue_detection` keep validating. New agents being ported
    declare what they actually need explicitly.
    """
    model_config = ConfigDict(extra="forbid")

    enable_rogue_detection: bool = Field(
        default=True,
        description="Turn on agent_os RogueDetectionMiddleware.",
    )
    enable_prompt_injection_guard: bool = Field(default=True)
    enable_credential_redactor: bool = Field(default=True)
    credential_mode: Literal["redact", "deny"] = Field(default="redact")
    enable_context_budget: bool = Field(default=True)
    context_budget_tokens: int = Field(
        default=8000, gt=0, le=200_000,
        description="Total token budget the ContextScheduler hands out per agent.",
    )
    prompt_injection_block_threshold: Literal["medium", "high", "critical"] = Field(
        default="medium",
        description="Minimum threat level at which the PromptInjectionGuard blocks. "
                    "Pipeline agents that receive trusted LLM-generated markdown should "
                    "use 'high' to avoid false positives on --- separators and code fences.",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Tool function names the CapabilityGuard will permit. "
                    "Must include the __name__ of every callable passed as `tools=` to build_agent().",
    )
    denied_tools: list[str] = Field(default_factory=list)

    # ── WS7 gap-module toggles (consumed by the LangGraph axis,
    #    agent_framework_adapters/langgraph/governance.build_langgraph_governance) ──────────
    blocked_patterns: list[str] = Field(
        default_factory=list,
        description="Substrings denied in tool arguments / model output "
                    "(e.g. 'DROP TABLE'). Cheap allowlist-style content guard.",
    )
    enable_data_fgac: bool = Field(
        default=False,
        description="Gap 1 — route the agent's data reads through the FGAC "
                    "DataAccessMediator (ABAC mask/row-filter/deny per NHI scope).",
    )
    enable_data_drift: bool = Field(
        default=False,
        description="Gap 3 — feed each data read to the DataAccessDriftDetector "
                    "(volume/sensitivity/new-table drift → quarantine).",
    )
    enable_reasoning_guard: bool = Field(
        default=False,
        description="Gap 4 — validate planned tool/data-access steps against the "
                    "capability allow-list + data scope before execution.",
    )
    enable_reasoning_trace: bool = Field(
        default=False,
        description="Gap 4+ — capture CoT/CoVe with mandatory redaction → span "
                    "event + hash-stamped audit entry.",
    )


class AgentConfigModel(BaseModel):
    """One validated agent config."""
    model_config = ConfigDict(extra="forbid")

    agent_type: str = Field(
        pattern=r"^[A-Za-z][A-Za-z0-9]*$",
        description="PascalCase agent type (Scanner, ASTAnalyzer, Coder, ...). "
                    "The filesystem presence of the YAML is the source of truth — "
                    "no hardcoded enum is enforced here.",
    )
    description: Optional[str] = Field(default=None)
    max_file_scan_bytes: int = Field(
        gt=0, le=1_000_000,
        description="Per-file cap for content peeks (entry-point detection, etc).",
    )
    prompt_file: str = Field(
        description="Path to the system-prompt markdown file, relative to the "
                    "`agents/` package root (e.g. 'prompts/scanner.md'). "
                    "Required — agents/_base.py reads this at build time so the "
                    "prompt is the sole source of truth and version-controlled.",
    )
    shared_prompt_files: list[str] = Field(
        default_factory=list,
        description="Optional shared prompt fragments concatenated BEFORE "
                    "prompt_file (in declared order). Use to inject a "
                    "platform-wide preamble (e.g. 'prompts/_shared/quality-principles.md') "
                    "without duplicating it in every agent prompt. Pattern from "
                    "agentrepo/ms-agent-harness/prompts/quality-principles.md.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Optional per-agent model override. Falls back to "
                    "AZURE_OPENAI_DEPLOYMENT env when unset. Lets the porting "
                    "plan ship per-role models (gpt-5-3-codex for Coder, "
                    "gpt-5.4-mini for Reviewer) without code changes.",
    )
    max_output_tokens: Optional[int] = Field(
        default=None, gt=0, le=128_000,
        description="Per-agent cap on completion tokens. Forwarded to the "
                    "Responses API as default_options.max_output_tokens. "
                    "Stops a runaway model from blowing the budget; tune per role "
                    "(Reviewer: 4000, Coder: 12000, etc).",
    )
    a2a: A2AConfig
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)


# ── Loader ───────────────────────────────────────────────────────────────────

class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or fails schema."""


def load_agent_config(
    agent_name: str,
    config_dir: Optional[Path] = None,
) -> AgentConfigModel:
    """
    Load `<config_dir>/<agent_name_normalized>.yaml` and validate.

    Normalization: "scanner" → scanner.yaml; "ASTAnalyzer" and "ast-analyzer"
    both → ast_analyzer.yaml.

    Raises ConfigError on missing file, YAML parse failure, or schema mismatch.
    """
    if config_dir is None:
        config_dir = Path(__file__).parent / "config"

    normalized = agent_name.lower().replace("-", "_")
    # Also accept PascalCase -> snake_case (ASTAnalyzer -> astanalyzer; not ideal
    # but the file is named ast_analyzer.yaml so we convert the hyphen form).
    config_path = config_dir / f"{normalized}.yaml"

    if not config_path.exists():
        raise ConfigError(
            f"Agent config not found: {config_path}. "
            f"Expected a .yaml file in {config_dir} matching the agent name."
        )

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top-level must be a mapping, got {type(raw).__name__}")

    # The on-disk format keeps nested agent/a2a/governance sections; flatten
    # the agent block into the model's top level. Missing fields are NOT
    # defaulted here — Pydantic decides what's required.
    agent_block = raw.get("agent") or {}
    if not isinstance(agent_block, dict):
        raise ConfigError(f"{config_path}: 'agent' must be a mapping")

    model_input = {
        **agent_block,                      # agent_type, description, max_file_scan_bytes
        "a2a": raw.get("a2a") or {},
        "governance": raw.get("governance") or {},
    }
    # Rename 'type' -> 'agent_type' so YAML reads naturally under `agent:`.
    if "type" in model_input and "agent_type" not in model_input:
        model_input["agent_type"] = model_input.pop("type")

    try:
        return AgentConfigModel.model_validate(model_input)
    except ValidationError as e:
        raise ConfigError(
            f"Schema validation failed for {config_path}:\n"
            + "\n".join(f"  - {'/'.join(str(p) for p in err['loc'])}: {err['msg']}"
                        for err in e.errors())
        ) from e


# ── Per-process cache ────────────────────────────────────────────────────────

_config_cache: dict[str, AgentConfigModel] = {}


def load_agent_config_cached(agent_name: str) -> AgentConfigModel:
    """Load once per agent_name per process. Safe to call from module scope."""
    key = agent_name.lower().replace("-", "_")
    if key not in _config_cache:
        _config_cache[key] = load_agent_config(agent_name)
    return _config_cache[key]


def clear_config_cache() -> None:
    """Test-only: reset the per-process cache."""
    _config_cache.clear()
