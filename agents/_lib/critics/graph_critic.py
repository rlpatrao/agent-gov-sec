"""Validate a DependencyGraph against a deterministic re-scan of source."""
from __future__ import annotations

from pathlib import Path

from core.discovery_artifacts import CriticReport, DependencyGraph, Inventory
from agents._lib.discovery_tools.aws_sdk_patterns import resolve
from agents._lib.discovery_tools.tree_sitter_py import extract_boto3_calls, parse_imports


def critique_graph(
    graph: DependencyGraph,
    repo_root: Path,
    inventory: Inventory,
) -> CriticReport:
    repo_root = Path(repo_root).resolve()
    edges = {(e.src, e.dst, e.kind) for e in graph.edges}
    reasons: list[str] = []
    suggestions: list[str] = []
    module_ids = {m.id for m in inventory.modules}

    for m in inventory.modules:
        for py_file in (repo_root / m.path).rglob("*.py"):
            for imp in parse_imports(str(py_file)):
                if imp.module.startswith("."):
                    continue
                head = imp.module.split(".", 1)[0]
                if head in module_ids and head != m.id:
                    if (m.id, head, "imports") not in edges:
                        reasons.append(
                            f"missing import edge {m.id} -> {head} ({py_file}:{imp.line})"
                        )
            for call in extract_boto3_calls(str(py_file)):
                ref = resolve(call)
                if ref is None:
                    continue
                target = f"{ref.kind}:{ref.name}" if ref.name else None
                if target is None:
                    continue
                edge = (m.id, target, ref.access)
                if edge not in edges:
                    reasons.append(
                        f"missing edge {m.id} -[{ref.access}]-> {target} "
                        f"(call {call.service}.{call.method} at {py_file}:{call.line})"
                    )

    if reasons:
        suggestions.append("Re-run grapher; ensure aws_sdk_patterns covers each call.")
    return CriticReport(
        verdict="PASS" if not reasons else "FAIL",
        reasons=reasons,
        suggestions=suggestions,
    )
