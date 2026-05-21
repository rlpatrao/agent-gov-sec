"""Build, serialize, and load a DependencyGraph.

Also emits .dot, .mmd, and (when graphviz is on PATH) .svg alongside the JSON.

Ported from agentrepo discovery/tools/graph_io.py — only the import path for
DependencyGraph changed (now core.discovery_artifacts).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from core.discovery_artifacts import DependencyGraph, GraphEdge, GraphNode

_logger = logging.getLogger("discovery.graph_io")


class GraphBuilder:
    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: set[tuple[str, str, str]] = set()

    def add_module(self, module_id: str, attrs: dict | None = None) -> str:
        if module_id not in self._nodes:
            self._nodes[module_id] = GraphNode(id=module_id, kind="module", attrs=attrs or {})
        return module_id

    def add_library(self, lib_id: str, attrs: dict | None = None) -> str:
        if lib_id not in self._nodes:
            self._nodes[lib_id] = GraphNode(id=lib_id, kind="library", attrs=attrs or {})
        return lib_id

    def add_resource(self, kind: str, name: str | None, attrs: dict | None = None) -> str:
        node_id = f"{kind}:{name}" if name else f"{kind}:<unknown:{uuid.uuid4().hex[:8]}>"
        if node_id not in self._nodes:
            merged = {"resource_kind": kind, **(attrs or {})}
            if name:
                merged["resource_name"] = name
            self._nodes[node_id] = GraphNode(id=node_id, kind="aws_resource", attrs=merged)
        return node_id

    def add_edge(self, src: str, dst: str, kind: str) -> None:
        self._edges.add((src, dst, kind))

    def build(self) -> DependencyGraph:
        return DependencyGraph(
            nodes=list(self._nodes.values()),
            edges=[GraphEdge(src=s, dst=d, kind=k) for (s, d, k) in sorted(self._edges)],
        )


def save(graph: DependencyGraph, path: Path) -> None:
    """Write JSON + DOT + Mermaid; render SVG if graphviz available."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(graph.model_dump_json(indent=2), encoding="utf-8")
    path.with_suffix(".dot").write_text(to_dot(graph), encoding="utf-8")
    path.with_suffix(".mmd").write_text(to_mermaid(graph), encoding="utf-8")
    if shutil.which("dot"):
        svg_path = path.with_suffix(".svg")
        try:
            subprocess.run(
                ["dot", "-Tsvg", str(path.with_suffix(".dot")), "-o", str(svg_path)],
                check=True, capture_output=True, timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            _logger.warning("graphviz render failed: %s", exc)


def load(path: Path) -> DependencyGraph:
    return DependencyGraph.model_validate_json(Path(path).read_text(encoding="utf-8"))


_NODE_STYLE = {
    "module":       {"shape": "box",      "style": "filled", "fillcolor": "#cde4ff"},
    "library":      {"shape": "folder",   "style": "filled", "fillcolor": "#e8e8e8"},
    "aws_resource": {"shape": "cylinder", "style": "filled", "fillcolor": "#ffe0b3"},
}

_EDGE_COLOR = {
    "imports":  "#666666",
    "reads":    "#1a7f37",
    "writes":   "#b35900",
    "produces": "#9a00cc",
    "consumes": "#005f99",
    "invokes":  "#cc0000",
}


def _safe_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", raw) or "n"


def _node_label(node: GraphNode) -> str:
    if node.kind == "module":
        return node.id
    if node.kind == "library":
        return f"lib:{node.id}"
    rk = node.attrs.get("resource_kind", "aws")
    rn = node.attrs.get("resource_name", node.id.split(":", 1)[-1])
    return f"{rk}\n{rn}"


def to_dot(graph: DependencyGraph) -> str:
    lines = ["digraph G {", "  rankdir=LR;",
             '  node [fontname="Helvetica" fontsize=10];',
             '  edge [fontname="Helvetica" fontsize=9];']
    for n in graph.nodes:
        style = _NODE_STYLE.get(n.kind, _NODE_STYLE["aws_resource"])
        attrs = " ".join(f'{k}="{v}"' for k, v in style.items())
        label = _node_label(n).replace('"', '\\"').replace("\n", "\\n")
        lines.append(f'  {_safe_id(n.id)} [label="{label}" {attrs}];')
    for e in graph.edges:
        color = _EDGE_COLOR.get(e.kind, "#000000")
        lines.append(
            f'  {_safe_id(e.src)} -> {_safe_id(e.dst)} '
            f'[label="{e.kind}" color="{color}" fontcolor="{color}"];'
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def to_mermaid(graph: DependencyGraph) -> str:
    lines = ["```mermaid", "flowchart LR"]
    style_classes: dict[str, list[str]] = {"module": [], "library": [], "resource": []}
    for n in graph.nodes:
        label = _node_label(n).replace("\n", " · ")
        nid = _safe_id(n.id)
        if n.kind == "module":
            lines.append(f"    {nid}[{label}]")
            style_classes["module"].append(nid)
        elif n.kind == "library":
            lines.append(f"    {nid}[/{label}/]")
            style_classes["library"].append(nid)
        else:
            lines.append(f"    {nid}[({label})]")
            style_classes["resource"].append(nid)
    for e in graph.edges:
        lines.append(f"    {_safe_id(e.src)} -->|{e.kind}| {_safe_id(e.dst)}")
    lines.append("    classDef module fill:#cde4ff,stroke:#4b7bec;")
    lines.append("    classDef library fill:#e8e8e8,stroke:#888;")
    lines.append("    classDef resource fill:#ffe0b3,stroke:#b35900;")
    for cls, ids in style_classes.items():
        if ids:
            lines.append(f"    class {','.join(ids)} {cls};")
    lines.append("```")
    return "\n".join(lines) + "\n"
