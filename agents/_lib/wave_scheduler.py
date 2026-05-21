"""Deterministic topological layering of stories into migration waves.

No LLM. Cycle = hard error naming the cycle members.

Ported from agentrepo discovery/wave_scheduler.py — only import paths changed.
"""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from core.discovery_artifacts import (
    Backlog, BacklogItem, DependencyGraph, Inventory, Stories,
)


class CycleError(ValueError):
    pass


def schedule(
    stories: Stories,
    language_by_module: dict[str, str],
    inventory: Inventory | None = None,
    graph: DependencyGraph | None = None,
) -> Backlog:
    by_id = {s.id: s for s in stories.stories}

    for s in stories.stories:
        for dep in s.depends_on:
            if dep not in by_id:
                raise ValueError(f"Story {s.id} depends_on unknown story id: {dep}")

    indeg: dict[str, int] = {sid: 0 for sid in by_id}
    succ: dict[str, list[str]] = defaultdict(list)
    for s in stories.stories:
        for dep in s.depends_on:
            indeg[s.id] += 1
            succ[dep].append(s.id)

    epic_module: dict[str, str] = {e.id: e.module_id for e in stories.epics}

    layer_of: dict[str, int] = {}
    queue = deque(sorted(sid for sid, d in indeg.items() if d == 0))
    while queue:
        sid = queue.popleft()
        my_layer = max((layer_of[d] for d in by_id[sid].depends_on), default=0) + 1
        layer_of[sid] = my_layer
        for n in succ[sid]:
            indeg[n] -= 1
            if indeg[n] == 0:
                queue.append(n)

    if len(layer_of) != len(by_id):
        unresolved = sorted(set(by_id) - set(layer_of))
        raise CycleError(f"Cycle detected involving stories: {unresolved}")

    backlog_modules = {epic_module.get(s.epic_id, s.epic_id) for s in stories.stories}
    src_by_mod, ctx_by_mod = _compute_paths(inventory, graph, backlog_modules)

    items: list[BacklogItem] = []
    for sid in sorted(by_id, key=lambda x: (layer_of[x], x)):
        s = by_id[sid]
        module = epic_module.get(s.epic_id, s.epic_id)
        ac = "\n".join(c.text for c in s.acceptance_criteria)
        items.append(BacklogItem(
            module=module,
            language=language_by_module.get(module, "python"),
            work_item_id=s.id,
            title=s.title,
            description=s.description,
            acceptance_criteria=ac,
            source_paths=src_by_mod.get(module, []),
            context_paths=ctx_by_mod.get(module, []),
            wave=layer_of[sid],
        ))
    return Backlog(items=items)


def _compute_paths(
    inventory: Inventory | None,
    graph: DependencyGraph | None,
    backlog_modules: set[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    src: dict[str, list[str]] = {}
    ctx: dict[str, list[str]] = {}
    if inventory is None:
        return src, ctx

    root = Path(inventory.repo_meta.root_path).resolve()
    inv_by_id = {m.id: m for m in inventory.modules}

    for m in inventory.modules:
        if m.id not in backlog_modules:
            continue
        src[m.id] = [str((root / m.handler_entrypoint).resolve())]
        ctx[m.id] = []

    if graph is None:
        return src, ctx

    for edge in graph.edges:
        if edge.kind != "imports":
            continue
        if edge.src not in backlog_modules:
            continue
        if edge.dst in backlog_modules:
            continue
        dst_dir = (root / inv_by_id[edge.dst].path).resolve() \
            if edge.dst in inv_by_id else (root / edge.dst).resolve()
        if not dst_dir.is_dir():
            continue
        for py in sorted(dst_dir.rglob("*.py")):
            ctx[edge.src].append(str(py.resolve()))

    for k, vs in ctx.items():
        seen: set[str] = set()
        ctx[k] = [v for v in vs if not (v in seen or seen.add(v))]

    return src, ctx
