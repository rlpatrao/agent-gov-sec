"""Deterministic story critic — DAG well-formedness and module coverage."""
from __future__ import annotations

from collections import defaultdict, deque

from core.discovery_artifacts import CriticReport, Inventory, Stories


def critique_stories(stories: Stories, inventory: Inventory) -> CriticReport:
    reasons: list[str] = []
    by_id = {s.id: s for s in stories.stories}

    for s in stories.stories:
        if not s.acceptance_criteria:
            reasons.append(f"story {s.id}: must have at least one acceptance criterion")
        for dep in s.depends_on:
            if dep not in by_id:
                reasons.append(f"story {s.id}: depends_on unknown story {dep}")

    epic_modules = {e.module_id for e in stories.epics}
    for m in inventory.modules:
        if m.id not in epic_modules:
            reasons.append(f"module {m.id}: no epic produced")

    indeg = {sid: 0 for sid in by_id}
    succ: dict[str, list[str]] = defaultdict(list)
    for s in stories.stories:
        for dep in s.depends_on:
            if dep in by_id:
                indeg[s.id] += 1
                succ[dep].append(s.id)
    q = deque(sid for sid, d in indeg.items() if d == 0)
    seen = 0
    resolved: set[str] = set()
    while q:
        sid = q.popleft()
        seen += 1
        resolved.add(sid)
        for n in succ[sid]:
            indeg[n] -= 1
            if indeg[n] == 0:
                q.append(n)
    if seen != len(by_id):
        unresolved = sorted(set(by_id) - resolved)
        reasons.append(f"cycle in story dependency graph involving: {unresolved}")

    return CriticReport(
        verdict="PASS" if not reasons else "FAIL",
        reasons=reasons,
        suggestions=[],
    )
