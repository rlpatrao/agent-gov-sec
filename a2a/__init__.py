"""
Galaxy A2A — structured agent-to-agent messaging.

Every cross-agent call in the Galaxy platform goes through an `A2ARequest`
envelope. The envelope carries provenance (sender, recipient, conversation
id, causation chain), a declared payload schema, and the payload itself.

The dispatcher (`a2a.dispatcher.a2a_call`) is the single entrypoint: it
logs a `a2a_dispatch` governance event on the sender, opens an OTel child
span for the recipient, invokes the recipient's handler, and logs an
`a2a_reply` event with the outcome. Callers never construct replies or
spans by hand.
"""

from a2a.envelope import (
    A2AError,
    A2ARequest,
    A2AResponse,
    A2AStatus,
)
from a2a.dispatcher import a2a_call

__all__ = [
    "A2AError",
    "A2ARequest",
    "A2AResponse",
    "A2AStatus",
    "a2a_call",
]
