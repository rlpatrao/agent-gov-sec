"""Deterministic design critic — verifies every resource has a mapping."""
from __future__ import annotations

import re

from core.discovery_artifacts import (
    CriticReport, DependencyGraph, Inventory, ModuleBRD, ModuleDesign, SystemDesign,
)


def critique_designs(
    designs: list[ModuleDesign],
    system: SystemDesign,
    inventory: Inventory,
    graph: DependencyGraph,
    module_brds: list[ModuleBRD],
) -> CriticReport:
    reasons: list[str] = []
    by_id = {d.module_id: d for d in designs}

    for m in inventory.modules:
        d = by_id.get(m.id)
        if d is None:
            reasons.append(f"design missing for module {m.id}")
            continue
        module_resources = {e.dst for e in graph.edges if e.src == m.id and ":" in e.dst}
        state_section = _section_text(d.body, "State Mapping")
        for rid in module_resources:
            kind, _, name = rid.partition(":")
            if name and name not in state_section and kind not in state_section:
                reasons.append(
                    f"module {m.id}: resource {rid} not mapped in State Mapping section"
                )

    return CriticReport(
        verdict="PASS" if not reasons else "FAIL",
        reasons=reasons,
        suggestions=[],
    )


def _section_text(body: str, name: str) -> str:
    m = re.search(
        rf"^##\s+{re.escape(name)}\b.*?$(.*?)(?=^##\s|\Z)",
        body, flags=re.MULTILINE | re.DOTALL,
    )
    return (m.group(1) if m else "").strip()
