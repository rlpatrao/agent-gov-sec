"""
governance.extensions.mcp_substrate — shared persistence/contract substrate for
the MCP guard wrappers.

This is not a guard. The MCP guards (gateway, scanner) accept an ``audit_sink=``
so all MCP decisions land in one audit trail. ``make_mcp_audit_sink`` builds the
single ``InMemoryAuditSink`` (from ``agent_os.mcp_protocols``) that wiring code
passes to each consumer. For multi-process deployments, implement the
``MCPAuditSink`` Protocol against Redis/DB and inject that instead; the default
implementation is thread-safe but process-local and lost on restart.
"""

from __future__ import annotations

from agent_os.mcp_protocols import InMemoryAuditSink


def make_mcp_audit_sink() -> InMemoryAuditSink:
    """Construct one shared in-memory audit sink for the MCP guard wrappers.

    Pass the returned instance as ``audit_sink=`` to both ``MCPGateway`` and
    ``MCPSecurityScanner`` so the whole MCP decision path lands in one trail.
    Use ``.entries()`` to read the recorded decisions.
    """
    return InMemoryAuditSink()
