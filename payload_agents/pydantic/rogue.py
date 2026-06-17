"""payload_agents.pydantic.rogue — Rogue on the Pydantic AI framework.

shell_exec ToolSpec present on purpose; empty allow-list → every call denied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from payload_agents._lib import personas
from payload_agents.pydantic._runner import PydanticAgentBundle, build_agent


async def build_rogue_agent(
    run_id: str,
    model: Any,
    *,
    catalog=None,
    drift_baseline_path: Optional[Path] = None,
) -> PydanticAgentBundle:
    catalog = catalog or personas.load_catalog()
    mediator = personas.make_mediator(catalog, drift_baseline_path)
    return await build_agent("rogue", "Rogue", run_id, model=model, tool_specs=personas.rogue_specs(),
                             mediator=mediator, catalog=catalog)
