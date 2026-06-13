"""
governance.extensions.mcp_response_guard — per-call scan of INBOUND MCP tool
output before it reaches the model.

Wraps ``agent_os.mcp_response_scanner.MCPResponseScanner``. The scanner is
stateless (no constructor arguments) and inspects a tool result for five threat
categories: instruction_injection, prompt_injection, credential_leak, pii_leak,
and data_exfiltration. This guard belongs on the tool-result channel — the
AFTER_TOOL hook — not on outbound arguments.

``scan_result(name, result_text)`` returns a ``GuardDecision``:
  - allowed when the scan reports ``is_safe`` True;
  - blocked with ``code='mcp_response_unsafe'`` otherwise, carrying the detected
    threat categories in the reason and metadata, and the sanitized text in
    ``output`` so a SANITIZE-style caller can forward the stripped result.

Quirks honored (per discovery notes):
  - The scanner is fail-closed: ``scan_response`` wraps its work in try/except
    and returns an unsafe result on any internal error, so a non-safe result is
    always treated as a block here.
  - ``sanitize_response`` only strips instruction-tag patterns; it does NOT
    remove credential/PII/exfil spans. The guard therefore still blocks when the
    scan is unsafe even though ``output`` holds the (partially) sanitized text —
    the block is authoritative and the sanitized text is advisory metadata.

The wrapper is flag-agnostic and never imports the pipeline; the pipeline gates
it behind ``GALAXY_GAP_MCP_RESPONSE_SCAN`` and maps a block onto
GovernanceViolation.
"""

from __future__ import annotations

from typing import Optional

from agent_os.mcp_response_scanner import MCPResponseScanner

from governance.extensions.decision import GuardDecision


class McpResponseGuard:
    """Scans inbound MCP tool output for injection/exfil/leak threats."""

    def __init__(self) -> None:
        # MCPResponseScanner is stateless and takes no constructor arguments;
        # build one instance and reuse it across calls.
        self._scanner = MCPResponseScanner()

    def scan_result(self, name: str, result_text: Optional[str]) -> GuardDecision:
        """Return a GuardDecision for an inbound tool result.

        ``result_text`` is the raw string the MCP tool produced. The scanner is
        fail-closed, so any internal error surfaces as ``is_safe=False`` and is
        treated as a block.
        """
        scan = self._scanner.scan_response(result_text, name or "unknown")

        if scan.is_safe:
            return GuardDecision.allow(
                reason=f"MCP response from {name!r} clear of known threats",
                tool=name,
            )

        categories = sorted({threat.category for threat in scan.threats})
        # sanitize_response strips only instruction-tag spans; it does not remove
        # credential/PII/exfil content, so the block stands and the sanitized
        # text is advisory only.
        sanitized, _ = self._scanner.sanitize_response(result_text, name or "unknown")

        decision = GuardDecision.block(
            "mcp_response_unsafe",
            f"MCP response from {name!r} flagged: {', '.join(categories)}",
            signals=categories,
            tool=name,
            threat_categories=categories,
        )
        decision.output = sanitized
        return decision
