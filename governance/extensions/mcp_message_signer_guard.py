"""
governance.extensions.mcp_message_signer_guard — secures the MCP transport /
agent-to-agent message channel with HMAC signing and replay protection.

Wraps ``agent_os.mcp_message_signer.MCPMessageSigner``. Outbound, ``sign``
attaches a signed envelope; inbound, ``verify`` gates acceptance and returns a
``GuardDecision``. An invalid signature, a tampered payload, an out-of-window
timestamp, or a replayed nonce all surface as a fail-closed block; the wrapper
never raises ``GovernanceViolation``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from agent_os.mcp_message_signer import MCPMessageSigner, MCPSignedEnvelope

from governance.extensions.decision import GuardDecision


class McpMessageSignerGuard:
    """Signs and verifies MCP message envelopes.

    Constructor quirk workaround: ``MCPMessageSigner`` requires a signing key of
    at least 32 bytes or ``__init__`` raises ``ValueError``. When no key is
    supplied the constructor self-provisions one via ``generate_key()`` (32
    random bytes) so wiring never fails on key length. Replay protection is
    per-process via the in-memory LRU nonce store unless a shared
    ``MCPNonceStore`` is injected on the underlying signer.
    """

    def __init__(
        self,
        *,
        signing_key: Optional[bytes] = None,
        replay_window: timedelta = timedelta(minutes=5),
        signer: Optional[MCPMessageSigner] = None,
    ) -> None:
        if signer is not None:
            self._signer = signer
        else:
            key = signing_key if signing_key is not None else MCPMessageSigner.generate_key()
            self._signer = MCPMessageSigner(key, replay_window=replay_window)

    def sign(self, payload: str, sender: Optional[str] = None) -> MCPSignedEnvelope:
        """Attach a signed envelope to an outbound MCP payload."""
        return self._signer.sign_message(payload, sender)

    def verify(self, envelope: MCPSignedEnvelope) -> GuardDecision:
        """Verify an inbound envelope. is_valid False (bad signature, tamper,
        out-of-window, or replayed nonce) is fail-closed -> block."""
        result = self._signer.verify_message(envelope)
        if not result.is_valid:
            return GuardDecision.block(
                "mcp_signature_invalid",
                f"MCP message verification failed: {result.failure_reason}",
                signals=["mcp_message_signer"],
                failure_reason=result.failure_reason,
                sender_id=envelope.sender_id,
            )
        return GuardDecision.allow(
            reason="MCP message signature valid",
            sender_id=result.sender_id,
        )
