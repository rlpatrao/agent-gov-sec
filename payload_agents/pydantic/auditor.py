"""payload_agents.pydantic.auditor — Auditor on the Pydantic AI framework."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from payload_agents._lib import personas
from payload_agents.pydantic._runner import PydanticAgentBundle, build_agent


async def build_auditor_agent(
    run_id: str,
    model: Any,
    *,
    catalog=None,
    drift_baseline_path: Optional[Path] = None,
) -> PydanticAgentBundle:
    catalog = catalog or personas.load_catalog()
    mediator = personas.make_mediator(catalog, drift_baseline_path)
    specs = personas.auditor_specs(mediator=mediator, nhi_id="local-auditor-nhi")
    return await build_agent("auditor", "Auditor", run_id, model=model, tool_specs=specs, mediator=mediator, catalog=catalog)
