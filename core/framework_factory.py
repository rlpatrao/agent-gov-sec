"""
core.framework_factory — binds the neutral agent contract to a framework adapter.

The **framework axis**, orthogonal to ``core.provider_factory`` (the cloud axis).
Selection is by ``--framework`` / the ``GALAXY_FRAMEWORK`` env var (default
``langgraph``). The chosen adapter package is imported lazily, so a process that
runs ``--framework raw`` never imports LangChain, and importing this module pulls
no framework at all.

    from core.framework_factory import get_framework
    fw = get_framework()                      # langgraph by default
    bundle = await fw.build_agent("finops", run_id, ...)

Each ``adapters/<framework>/`` package must expose the builder surface the demo
uses (``build_agent`` / the per-persona builders) and return an object satisfying
``agent_framework_adapters.contract.AgentBundle`` (a framework-neutral ``invoke(prompt) ->
RunResult``).
"""

from __future__ import annotations

import importlib
import logging
import os
from types import ModuleType

logger = logging.getLogger(__name__)

# name -> adapter package
_FRAMEWORK_PACKAGES: dict[str, str] = {
    "langgraph": "agent_framework_adapters.langgraph",       # LangChain create_agent + middleware
    "raw": "agent_framework_adapters.raw",                   # provider-native tool loop, no framework
    "pydantic": "agent_framework_adapters.pydantic_ai",      # Pydantic AI Agent (native models)
}

DEFAULT_FRAMEWORK = "langgraph"

_cache: dict[str, ModuleType] = {}


def available_frameworks() -> list[str]:
    return sorted(_FRAMEWORK_PACKAGES)


def get_framework(name: str | None = None) -> ModuleType:
    """Resolve and import the selected framework adapter package. ``name``
    overrides ``GALAXY_FRAMEWORK`` / the default. Raises ``ValueError`` for an
    unknown name and ``ImportError`` when the adapter (or its deps) is absent."""
    name = (name or os.environ.get("GALAXY_FRAMEWORK") or DEFAULT_FRAMEWORK).lower()
    if name in _cache:
        return _cache[name]
    pkg = _FRAMEWORK_PACKAGES.get(name)
    if pkg is None:
        raise ValueError(f"Unknown framework={name!r}. Available: {available_frameworks()}")
    module = importlib.import_module(pkg)
    logger.info("framework_factory.selected", extra={"framework": name})
    _cache[name] = module
    return module
