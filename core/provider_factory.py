"""
core.provider_factory — binds the agnostic interfaces to a cloud adapter set.

Selection is by the ``CLOUD_PROVIDER`` env var (default ``azure``). The chosen
adapter package is imported lazily, so a process that runs with
``CLOUD_PROVIDER=azure`` never needs the AWS/GCP SDKs installed, and importing
this module pulls no cloud SDK at all.

    from core.provider_factory import get_provider
    provider = get_provider()                 # azure by default
    gateway  = provider.llm_gateway()

Each ``cloud_adapters/<cloud>/`` package must expose a module-level ``PROVIDER``
implementing ``core.interfaces.CloudProvider``. The AWS/GCP packages expose a
provider whose accessors raise ``NotImplementedError`` (WS5/WS6) so the
contract is locked but the impl is deferred.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.interfaces import CloudProvider

logger = logging.getLogger(__name__)

# name -> adapter package exposing `PROVIDER`
_PROVIDER_PACKAGES: dict[str, str] = {
    "azure": "cloud_adapters.azure",
    "aws": "cloud_adapters.aws",
    "gcp": "cloud_adapters.gcp",
    "local": "cloud_adapters.local",   # cloud-neutral: env secrets, in-memory ledger, no egress
}

DEFAULT_PROVIDER = "azure"

_cache: dict[str, "CloudProvider"] = {}


def available_providers() -> list[str]:
    return sorted(_PROVIDER_PACKAGES)


def get_provider(name: str | None = None) -> "CloudProvider":
    """Resolve the cloud provider. ``name`` overrides ``CLOUD_PROVIDER`` /
    the default. The adapter package is imported on first use and cached."""
    name = (name or os.environ.get("CLOUD_PROVIDER") or DEFAULT_PROVIDER).lower()
    if name in _cache:
        return _cache[name]
    pkg = _PROVIDER_PACKAGES.get(name)
    if pkg is None:
        raise ValueError(
            f"Unknown CLOUD_PROVIDER={name!r}. Available: {available_providers()}"
        )
    try:
        module = importlib.import_module(pkg)
    except ImportError as e:
        raise ImportError(
            f"Cloud adapter '{name}' could not be imported ({pkg}). "
            f"Install its optional dependencies, e.g. pip install '.[{name}]'. "
            f"Underlying error: {e}"
        ) from e
    try:
        provider = module.PROVIDER
    except AttributeError as e:
        raise AttributeError(
            f"Adapter package {pkg} does not expose a module-level PROVIDER."
        ) from e
    logger.info("provider_factory.selected", extra={"provider": name})
    _cache[name] = provider
    return provider
