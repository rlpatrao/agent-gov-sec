"""Locating and validating the governance configuration bundle.

The guards are only as good as the configuration behind them. A detector built
without its rule file does not fail — it falls back to whatever sample rules the
upstream toolkit ships, which is a weaker control wearing the same name. That is
the failure mode this module exists to prevent.

:func:`verify_bundle` is called during :func:`galaxy_agentkit.govern`, so an agent
whose configuration did not ship refuses to start instead of running degraded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


@dataclass(frozen=True)
class ConfigFile:
    """One required file, and what silently breaks when it is absent."""

    relative_path: str
    purpose: str


# Every file the in-process guards read from the installed package. Each one is
# required: the guard that reads it either weakens or fails without it, and a
# weakened guard is worse than an absent one because it still reports success.
REQUIRED_CONFIGS: tuple[ConfigFile, ...] = (
    ConfigFile(
        "configs/prompt-injection.yaml",
        "prompt-injection rules; without it the detector silently falls back to "
        "the toolkit's built-in sample patterns",
    ),
    ConfigFile(
        "configs/egress-gap.yaml",
        "egress allow-list gap rules used by the egress guard",
    ),
    ConfigFile("policies/galaxy-core.yaml", "core declarative policy rules"),
    ConfigFile("policies/galaxy-pii.yaml", "PII detection and redaction rules"),
    ConfigFile("policies/galaxy-tools.yaml", "tool capability rules"),
    ConfigFile("policies/galaxy-ast.yaml", "code/AST safety rules"),
    ConfigFile(
        "shared/enforcement/configs/authz.cedar",
        "Cedar authorization policies for the standards-based policy engine",
    ),
    ConfigFile(
        "shared/enforcement/configs/data-classification.example.yaml",
        "field-grained data classification catalogue driving mask/row-filter/deny",
    ),
)


def governance_root() -> Path:
    """Directory of the installed ``governance`` package."""
    try:
        import governance
    except ModuleNotFoundError as exc:  # pragma: no cover - install is broken
        raise ConfigurationError(
            "the `governance` package is not importable; the agentkit install is "
            "incomplete. Reinstall with `pip install galaxy-agentkit`."
        ) from exc
    if not governance.__file__:
        raise ConfigurationError("the `governance` package has no filesystem location")
    return Path(governance.__file__).parent


def missing_configs() -> list[ConfigFile]:
    """Required configuration files that are not present in the install."""
    root = governance_root()
    return [c for c in REQUIRED_CONFIGS if not (root / c.relative_path).is_file()]


def verify_bundle() -> Path:
    """Confirm the full configuration bundle shipped; raise if it did not.

    Returns the governance package root so callers can resolve paths from it.
    """
    missing = missing_configs()
    if not missing:
        return governance_root()

    root = governance_root()
    lines = [
        f"{len(missing)} of {len(REQUIRED_CONFIGS)} required governance "
        "configuration files are missing from the installed package.",
        "",
        "Refusing to start: the guards that read these files do not fail without "
        "them, they degrade — the prompt-injection detector in particular falls "
        "back to sample rules and still reports success. An agent running on "
        "sample rules while appearing governed is the outcome this check exists "
        "to prevent.",
        "",
        f"Looked under: {root}",
        "",
        "Missing:",
    ]
    lines += [f"  - {c.relative_path}\n      {c.purpose}" for c in missing]
    lines += [
        "",
        "This normally means the wheel was built without its package data. "
        "Verify with:",
        "  python -c \"import galaxy_agentkit; galaxy_agentkit.check_install()\"",
    ]
    raise ConfigurationError("\n".join(lines))


def config_path(relative_path: str) -> Path:
    """Absolute path to one configuration file, raising if it is absent."""
    path = governance_root() / relative_path
    if not path.is_file():
        raise ConfigurationError(
            f"governance configuration {relative_path!r} is missing from the "
            f"installed package (looked at {path})."
        )
    return path
