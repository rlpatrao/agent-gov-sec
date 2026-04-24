"""
a2a.envelope — typed message envelopes for agent-to-agent calls.

Design goals:
  - Every A2A call carries explicit provenance (who sent it, who should
    receive it, which run/conversation it belongs to, what caused it).
  - Payload is a plain dict but the *shape* is declared via `payload_schema`
    so governance middleware and audit tooling can reason about what's
    flowing between agents without parsing free-form text.
  - Serialization is pure-JSON: the ledger stores envelopes verbatim,
    and replaying a run means replaying a list of envelopes.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class A2AStatus(str, Enum):
    """Terminal status codes for an A2A reply."""
    OK = "ok"                    # handler returned a payload
    DENIED = "denied"            # governance blocked the call (policy or sender unknown)
    ERROR = "error"              # handler raised; payload holds `error` field
    TIMEOUT = "timeout"          # handler exceeded its deadline


@dataclass
class A2AError:
    """Structured error carried in A2AResponse.payload when status != ok."""
    code: str                    # e.g. "policy_denied", "handler_raised", "schema_mismatch"
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class A2ARequest:
    """
    Envelope sent from one agent to another.

    Fields:
      conversation_id  — stable id for the whole Scanner→AST→… chain; re-use
                         across reply envelopes so audit rows correlate.
      message_id       — unique per envelope; the reply's `in_reply_to` points
                         here.
      in_reply_to      — set on replies or follow-ups; None for the first
                         request in a conversation.
      sender           — NHI-qualified agent id of the caller, e.g.
                         "Scanner-local-scanner-nhi".
      recipient        — NHI-qualified agent id of the callee.
      run_id           — Galaxy run id the call belongs to (matches audit +
                         ledger `run_id`).
      module_id        — the module under analysis.
      intent           — short verb phrase ("analyze_ast", "summarize_diff").
      payload_schema   — name of the declared payload shape, e.g. "ASTRequest/v1".
      payload          — the actual message body; plain JSON-serialisable dict.
      created_at       — unix seconds; set automatically.
    """

    conversation_id: str
    sender: str
    recipient: str
    run_id: str
    module_id: str
    intent: str
    payload_schema: str
    payload: dict = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: f"msg-{uuid.uuid4().hex[:12]}")
    in_reply_to: Optional[str] = None
    created_at: float = field(default_factory=lambda: time.time())

    @classmethod
    def new(
        cls,
        sender: str,
        recipient: str,
        run_id: str,
        module_id: str,
        intent: str,
        payload_schema: str,
        payload: dict,
        conversation_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> "A2ARequest":
        """Construct a new request, auto-minting a conversation_id if missing."""
        return cls(
            conversation_id=conversation_id or f"conv-{uuid.uuid4().hex[:12]}",
            sender=sender,
            recipient=recipient,
            run_id=run_id,
            module_id=module_id,
            intent=intent,
            payload_schema=payload_schema,
            payload=payload,
            in_reply_to=in_reply_to,
        )

    def validate(self) -> None:
        """Raise ValueError if the envelope is missing required provenance."""
        for field_name in ("conversation_id", "sender", "recipient",
                           "run_id", "module_id", "intent", "payload_schema"):
            if not getattr(self, field_name):
                raise ValueError(f"A2ARequest: missing required field '{field_name}'")
        if not isinstance(self.payload, dict):
            raise ValueError("A2ARequest: payload must be a dict")

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, sort_keys=True)


@dataclass
class A2AResponse:
    """
    Envelope returned by a recipient.

    `status` is the machine-readable outcome; `payload` carries the response
    body (or an `A2AError` serialised via `to_dict()` when status != ok).
    """

    conversation_id: str
    in_reply_to: str
    sender: str                      # the original recipient, replying
    recipient: str                   # the original sender
    run_id: str
    module_id: str
    status: A2AStatus
    payload_schema: str
    payload: dict = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: f"msg-{uuid.uuid4().hex[:12]}")
    latency_ms: float = 0.0
    created_at: float = field(default_factory=lambda: time.time())

    @classmethod
    def ok(
        cls,
        request: A2ARequest,
        payload: dict,
        payload_schema: str,
        latency_ms: float,
    ) -> "A2AResponse":
        return cls(
            conversation_id=request.conversation_id,
            in_reply_to=request.message_id,
            sender=request.recipient,
            recipient=request.sender,
            run_id=request.run_id,
            module_id=request.module_id,
            status=A2AStatus.OK,
            payload_schema=payload_schema,
            payload=payload,
            latency_ms=latency_ms,
        )

    @classmethod
    def error(
        cls,
        request: A2ARequest,
        error: A2AError,
        status: A2AStatus = A2AStatus.ERROR,
        latency_ms: float = 0.0,
    ) -> "A2AResponse":
        return cls(
            conversation_id=request.conversation_id,
            in_reply_to=request.message_id,
            sender=request.recipient,
            recipient=request.sender,
            run_id=request.run_id,
            module_id=request.module_id,
            status=status,
            payload_schema="A2AError/v1",
            payload=error.to_dict(),
            latency_ms=latency_ms,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, sort_keys=True)

    @property
    def is_ok(self) -> bool:
        return self.status == A2AStatus.OK
