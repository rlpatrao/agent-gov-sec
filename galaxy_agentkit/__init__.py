"""galaxy_agentkit — the client-side package for building governed agents.

This is what an agent team installs and imports. It gives an agent one handle
that carries its identity, talks to the governance enforcement service, and
optionally runs the guard pipeline in-process for defence in depth.

    from galaxy_agentkit import govern

    agent = govern()
    reply = agent.llm([{"role": "user", "content": [{"text": "..."}]}])

What the kit will not do is start an agent it cannot govern. Missing identity,
a missing endpoint, an unreachable authority, or an incomplete configuration
bundle each raise at construction. There is no degraded mode, because a guard
running on fallback rules still reports success and is therefore worse than one
that is plainly absent.

Scaffold a new project with::

    galaxy init my-agent

See ``docs/shared/agentkit.md`` for the full integration guide.
"""

from __future__ import annotations

from .agent import GovernedAgent, govern
from .client import EnforcementClient
from .config import REQUIRED_CONFIGS, config_path, missing_configs, verify_bundle
from .errors import (
    ConfigurationError,
    EnforcementDenied,
    EnforcementUnavailable,
    GalaxyError,
)
from .settings import Settings

__version__ = "0.1.0"

__all__ = [
    "GovernedAgent",
    "govern",
    "EnforcementClient",
    "Settings",
    "GalaxyError",
    "ConfigurationError",
    "EnforcementDenied",
    "EnforcementUnavailable",
    "REQUIRED_CONFIGS",
    "config_path",
    "missing_configs",
    "verify_bundle",
    "check_install",
]


def check_install() -> bool:
    """Report whether the install carries a complete configuration bundle.

    Prints one line per required file and returns True when all are present.
    Intended for a post-install smoke check and for diagnosing a wheel that was
    built without its package data::

        python -c "import galaxy_agentkit; galaxy_agentkit.check_install()"
    """
    from .config import governance_root

    root = governance_root()
    missing = missing_configs()
    print(f"galaxy_agentkit {__version__}")
    print(f"governance package: {root}")
    for cfg in REQUIRED_CONFIGS:
        present = (root / cfg.relative_path).is_file()
        print(f"  [{'ok' if present else 'MISSING'}] {cfg.relative_path}")
    if missing:
        print(
            f"\n{len(missing)} file(s) missing — this install cannot govern an "
            "agent. The wheel was most likely built without its package data."
        )
        return False
    print("\nconfiguration bundle complete")
    return True
