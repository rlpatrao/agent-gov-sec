"""payload_agents.pydantic.finops — FinOpsAnalyst on the Pydantic AI framework.

Same persona and FGAC tool, governed by the same shared GuardPipeline — built via
the pydantic _runner (a GovernedModel wrapper around a native Pydantic AI model).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from payload_agents._lib import personas
from payload_agents.pydantic._runner import PydanticAgentBundle, build_agent


async def build_finops_agent(
    run_id: str,
    model: Any,
    *,
    catalog=None,
    drift_baseline_path: Optional[Path] = None,
) -> PydanticAgentBundle:
    catalog = catalog or personas.load_catalog()
    mediator = personas.make_mediator(catalog, drift_baseline_path)
    specs = personas.finops_specs(mediator=mediator, nhi_id="local-finops-nhi")
    return await build_agent("finops", "FinOps", run_id, model=model, tool_specs=specs, mediator=mediator, catalog=catalog)
