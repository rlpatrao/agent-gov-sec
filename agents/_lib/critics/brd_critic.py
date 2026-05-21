"""Deterministic BRD critic — checks coverage and required sections."""
from __future__ import annotations

import re

from core.discovery_artifacts import (
    CriticReport, DependencyGraph, Inventory, ModuleBRD, SystemBRD,
)

REQUIRED_SECTIONS = ("Business Rules", "Error Paths", "Side Effects")


def critique_brds(
    modules: list[ModuleBRD],
    system: SystemBRD,
    inventory: Inventory,
    graph: DependencyGraph,
) -> CriticReport:
    reasons: list[str] = []
    by_id = {b.module_id: b for b in modules}

    for m in inventory.modules:
        if m.id not in by_id:
            reasons.append(f"BRD missing for module {m.id}")

    for b in modules:
        for section in REQUIRED_SECTIONS:
            if not _section_has_content(b.body, section):
                reasons.append(f"module {b.module_id}: missing {section} section")

    # Only hold BRDs accountable for resources with known names.
    # Grapher placeholder IDs like `dynamodb_table:<unknown:abc123>` cannot be
    # cited by name in prose — including them forces critic failure on valid BRDs.
    resource_ids = {
        n.id for n in graph.nodes
        if n.kind == "aws_resource" and "<unknown:" not in n.id
    }
    referenced: set[str] = set()
    for b in modules:
        side = _section_text(b.body, "Side Effects")
        for rid in resource_ids:
            short = rid.split(":", 1)[-1]
            if short and short in side:
                referenced.add(rid)
    for rid in sorted(resource_ids - referenced):
        reasons.append(f"AWS resource {rid} not referenced by any BRD's Side Effects")

    return CriticReport(
        verdict="PASS" if not reasons else "FAIL",
        reasons=reasons,
        suggestions=[],
    )


def _section_text(body: str, name: str) -> str:
    pattern = rf"^##\s+{re.escape(name)}\b.*?$(.*?)(?=^##\s|\Z)"
    m = re.search(pattern, body, flags=re.MULTILINE | re.DOTALL)
    return (m.group(1) if m else "").strip()


def _section_has_content(body: str, name: str) -> bool:
    text = _section_text(body, name)
    for line in text.splitlines():
        line = line.strip()
        if line and line != "- " and not line.startswith("#"):
            return True
    return False
