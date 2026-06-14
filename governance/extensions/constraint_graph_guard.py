"""
governance.extensions.constraint_graph_guard — a per-call guard wrapping
``agent_os.constraint_graph.ConstraintGraph``.

The graph holds per-agent constraint edges (glob ``agent_pattern`` x glob
``resource`` -> ALLOW/DENY, with a priority) and answers an authorization
question via :meth:`ConstraintGraph.resolve`. It is deny-by-default: when no edge
matches, ``resolve`` returns ``False``. This wrapper is finer-grained than the
flat capability allow-list — it supports per-agent globs, conditions and
priority ordering.

This wrapper exposes a pure :meth:`check_tool` that returns a
:class:`GuardDecision` rather than raising, so the pipeline stays the single
place that maps a block onto a ``GovernanceViolation``. It does not import the
pipeline and it is flag-agnostic.

Quirk workarounds applied:

* ``resolve`` is called directly rather than going through
  ``ConstraintGraphEnforcer.intercept``; the enforcer does a deferred import of
  ``agent_os.integrations.base.ToolCallResult`` and requires a request object
  with ``.agent_id`` / ``.tool_name``, coupling we avoid here.
* Deny-by-default is fail-closed: an empty/misconfigured graph blocks every
  tool. The constructor seeds a default rule set (analyst-* may use read tools
  and ``database_query``; everyone is denied ``delete_*``) so the guard is
  enforceable out of the box, and a caller may pass an explicit ``edges`` list
  or a pre-built ``graph``.
"""

from __future__ import annotations

from typing import Any

from agent_os.constraint_graph import (
    ConstraintEdge,
    ConstraintGraph,
    Permission,
)

from governance.extensions.decision import GuardDecision


def _default_edges() -> list[ConstraintEdge]:
    return [
        ConstraintEdge(
            agent_pattern="analyst-*",
            resource="database_query",
            permission=Permission.ALLOW,
            priority=10,
        ),
        ConstraintEdge(
            agent_pattern="analyst-*",
            resource="read_*",
            permission=Permission.ALLOW,
            priority=10,
        ),
        # Deny edge wins on higher priority even when an allow edge also matches.
        ConstraintEdge(
            agent_pattern="*",
            resource="delete_*",
            permission=Permission.DENY,
            priority=20,
        ),
    ]


class ConstraintGraphGuard:
    """Wraps a :class:`ConstraintGraph` and returns a uniform verdict."""

    def __init__(
        self,
        edges: list[ConstraintEdge] | None = None,
        graph: ConstraintGraph | None = None,
    ) -> None:
        if graph is not None:
            self.graph = graph
        else:
            self.graph = ConstraintGraph()
            for edge in (edges if edges is not None else _default_edges()):
                self.graph.add_constraint(edge)

    def check_tool(
        self,
        agent_id: str,
        name: str,
        context: dict[str, Any] | None = None,
    ) -> GuardDecision:
        """Resolve a tool call against the constraint graph (deny-by-default)."""
        allowed = self.graph.resolve(agent_id, name, context=context)
        if not allowed:
            return GuardDecision.block(
                "constraint_denied",
                f"agent {agent_id!r} is not permitted to use tool {name!r} "
                f"(deny-by-default: no ALLOW edge matched, or a DENY edge won)",
                signals=["deny_by_default"],
                agent_id=agent_id,
                resource=name,
            )
        return GuardDecision.allow(
            f"agent {agent_id!r} permitted to use tool {name!r}",
            agent_id=agent_id,
            resource=name,
        )
