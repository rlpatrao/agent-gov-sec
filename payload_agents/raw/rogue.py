"""payload_agents.raw.rogue — Rogue on the raw (provider-native) framework.

Given a shell_exec ToolSpec on purpose; the empty allow-list means every call is
denied by the reasoning-step guard. Mediator is built so 'Rogue' deny-all applies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from payload_agents._lib import personas
from payload_agents.raw._runner import RawAgentBundle, build_agent


async def build_rogue_agent(
    run_id: str,
    model: Any,
    *,
    catalog=None,
    drift_baseline_path: Optional[Path] = None,
) -> RawAgentBundle:
    catalog = catalog or personas.load_catalog()
    mediator = personas.make_mediator(catalog, drift_baseline_path)
    return await build_agent("rogue", "Rogue", run_id, model=model, tool_specs=personas.rogue_specs(),
                             mediator=mediator, catalog=catalog)
