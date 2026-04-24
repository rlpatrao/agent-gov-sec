"""
agents/config.py

Load and validate per-agent configuration from YAML using Pydantic v2.

The YAML files in `agents/config/*.yaml` are the authoritative source of
per-agent tunables — nothing here should duplicate them as Python defaults.
If a required field is missing from the YAML, Pydantic raises — loud is good.

Usage:
    from agents.config import load_agent_config_cached

    config = load_agent_config_cached("scanner")
    config.agent_type                         # "Scanner"
    config.a2a.allowed_recipients             # ["ASTAnalyzer"]
    config.a2a.max_files_per_dispatch         # 40
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

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
    """Toggles for the per-agent governance middleware stack."""
    model_config = ConfigDict(extra="forbid")

    enable_rogue_detection: bool = Field(
        default=True,
        description="Turn on agent_os RogueDetectionMiddleware.",
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
