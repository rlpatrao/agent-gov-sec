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

By default a name resolves to its ``framework_adapters.<name>`` binding — the
platform surface. An application that exposes its own builder package (the
demo's personas, or a customer repo) passes it via ``package=`` or maps it with
``GALAXY_FRAMEWORK_PACKAGE_<NAME>``; builders return an object satisfying
``framework_adapters.contract.AgentBundle`` (a framework-neutral
``invoke(prompt) -> RunResult``). The platform never imports the application:
the dependency arrow points application -> platform.
"""

from __future__ import annotations

import importlib
import logging
import os
from types import ModuleType

logger = logging.getLogger(__name__)

# name -> the platform binding for that framework. Applications override per
# name (package= or GALAXY_FRAMEWORK_PACKAGE_<NAME>) to point at their own
# builder package; the map itself never names application code.
_FRAMEWORK_PACKAGES: dict[str, str] = {
    "langgraph": "framework_adapters.langgraph",   # LangChain create_agent + middleware
    "raw": "framework_adapters.raw",               # provider-native tool loop, no framework
    "pydantic": "framework_adapters.pydantic",     # Pydantic AI Agent (native models)
}

DEFAULT_FRAMEWORK = "langgraph"

_cache: dict[str, ModuleType] = {}


def available_frameworks() -> list[str]:
    return sorted(_FRAMEWORK_PACKAGES)


def get_framework(name: str | None = None, package: str | None = None) -> ModuleType:
    """Resolve and import the selected framework package. ``name`` overrides
    ``GALAXY_FRAMEWORK`` / the default. ``package`` (or the
    ``GALAXY_FRAMEWORK_PACKAGE_<NAME>`` env var) overrides the platform default
    with an application's own builder package. Raises ``ValueError`` for an
    unknown name and ``ImportError`` when the package (or its deps) is absent."""
    name = (name or os.environ.get("GALAXY_FRAMEWORK") or DEFAULT_FRAMEWORK).lower()
    pkg = (package
           or os.environ.get(f"GALAXY_FRAMEWORK_PACKAGE_{name.upper()}")
           or _FRAMEWORK_PACKAGES.get(name))
    if pkg is None:
        raise ValueError(f"Unknown framework={name!r}. Available: {available_frameworks()}")
    cache_key = f"{name}:{pkg}"
    if cache_key in _cache:
        return _cache[cache_key]
    module = importlib.import_module(pkg)
    logger.info("framework_factory.selected", extra={"framework": name, "package": pkg})
    _cache[cache_key] = module
    return module
