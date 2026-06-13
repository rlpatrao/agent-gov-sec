"""
governance.extensions.mcp_tool_screen — screens MCP tool DEFINITIONS
(name/description/schema) at server discovery/registration time, before any
tool is exposed to the model.

Wraps ``agent_os.mcp_security.MCPSecurityScanner``. This is not a per-(text)
GuardPipeline hook on tool arguments; it runs at MCP-server enumeration time so a
poisoned tool description never reaches the model. The wrapper exposes a pure
``screen_tool(...) -> GuardDecision`` and never raises ``GovernanceViolation``;
the pipeline maps a blocked decision onto a violation at the seam.

Fail-closed: the underlying ``scan_tool`` wraps its body in try/except and on any
error returns a single CRITICAL ``TOOL_POISONING`` threat, so an internal scan
error surfaces here as a block rather than a silent allow.
"""

from __future__ import annotations

import warnings
from typing import Any, Optional

from agent_os.mcp_security import (
    MCPSecurityScanner,
    MCPSecurityConfig,
    MCPThreat,
)

from governance.extensions.decision import GuardDecision


class McpToolScreen:
    """Screens MCP tool definitions for poisoning / hidden-instruction threats.

    Constructor quirk workaround: ``MCPSecurityScanner()`` with no ``config=``
    emits a ``warnings.warn`` at construction about sample rules (QUIRK 3). The
    documented config path (``examples/policies/mcp-security.yaml``) is optional
    and not present in every checkout, so by default the warning is suppressed
    rather than failing wiring. A caller that has a vetted policy file can pass a
    pre-built ``MCPSecurityConfig`` via ``config=``.
    """

    def __init__(
        self,
        *,
        scanner: Optional[MCPSecurityScanner] = None,
        config: Optional[MCPSecurityConfig] = None,
        audit_sink: Any = None,
    ) -> None:
        if scanner is not None:
            self._scanner = scanner
            return
        with warnings.catch_warnings():
            # QUIRK 3: suppress the construction-time sample-rules warning.
            warnings.simplefilter("ignore")
            self._scanner = MCPSecurityScanner(config=config, audit_sink=audit_sink)

    def screen_tool(
        self,
        name: str,
        description: str,
        schema: Optional[dict[str, Any]] = None,
        server: str = "unknown",
    ) -> GuardDecision:
        """Scan one tool definition. Allow when clean; block on any threat.

        CALLING QUIRK: ``scan_tool``'s 3rd positional arg is ``schema`` and the
        4th is ``server_name`` — ``server_name`` is always passed as a keyword so
        a stray string never hits the fail-closed schema path.
        """
        threats: list[MCPThreat] = self._scanner.scan_tool(
            name, description, schema, server_name=server
        )
        if not threats:
            return GuardDecision.allow(
                reason="MCP tool definition clean",
                tool=name,
                server=server,
            )
        threat_meta = [
            {
                "type": t.threat_type.value,
                "severity": t.severity.value,
                "message": t.message,
            }
            for t in threats
        ]
        types = sorted({t.threat_type.value for t in threats})
        return GuardDecision.block(
            "mcp_tool_poisoning",
            f"MCP tool '{name}' from '{server}' flagged {len(threats)} threat(s): "
            f"{', '.join(types)}",
            signals=["mcp_tool_screen"],
            tool=name,
            server=server,
            threats=threat_meta,
        )
