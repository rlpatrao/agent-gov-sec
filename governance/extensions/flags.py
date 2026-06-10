"""
governance.extensions.flags — feature flags for the WS7 gap modules.

Every gap module is **off by default**. Enable per-module via env var (truthy =
``1``/``true``/``yes``/``on``, case-insensitive). Centralised so the wiring layer
and tests share one source of truth.

  GALAXY_GAP_DATA_FGAC        → Gap 1  data-layer FGAC mediator
  GALAXY_GAP_DATA_DRIFT       → Gap 3  data-access drift detection
  GALAXY_GAP_REASONING_GUARD  → Gap 4  reasoning-step validation
  GALAXY_GAP_REASONING_TRACE  → Gap 4+ CoT/CoVe trace logging
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}

DATA_FGAC = "GALAXY_GAP_DATA_FGAC"
DATA_DRIFT = "GALAXY_GAP_DATA_DRIFT"
REASONING_GUARD = "GALAXY_GAP_REASONING_GUARD"
REASONING_TRACE = "GALAXY_GAP_REASONING_TRACE"


def is_enabled(flag: str) -> bool:
    """True if the named env flag is set to a truthy value. Default False."""
    return os.environ.get(flag, "").strip().lower() in _TRUTHY
