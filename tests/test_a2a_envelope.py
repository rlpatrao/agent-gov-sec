"""
A2A envelope + dispatcher contract tests.

Targets:
  - A2ARequest.validate rejects missing provenance
  - A2AResponse.ok / .error preserve the conversation/message linkage
  - a2a_call emits exactly two audit events (dispatch + reply) per hop
  - a2a_call DENIES when the recipient is not in `allowed_recipients`
  - a2a_call converts handler exceptions into status=ERROR replies AND
    re-raises so upstream breakers see the failure
"""

from __future__ import annotations

import pytest

from agent_os.audit_logger import AuditEntry, GovernanceAuditLogger, InMemoryBackend

from a2a.dispatcher import a2a_call
from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus


def _make_request(**overrides) -> A2ARequest:
    base = dict(
        sender="Scanner-local-scanner-nhi",
        recipient="ASTAnalyzer-local-ast-nhi",
        run_id="run-test-001",
        module_id="module-test",
        intent="analyze_ast",
        payload_schema="ASTRequest/v1",
        payload={"files": ["a.py", "b.py"]},
    )
    base.update(overrides)
    return A2ARequest.new(**base)


class _CapturingAudit(GovernanceAuditLogger):
    """Subclass we can pass to a2a_call to capture entries without patching."""
    def __init__(self) -> None:
        super().__init__()
        self.add_backend(InMemoryBackend())


# ── Envelope validation ───────────────────────────────────────────────────────

class TestEnvelope:
    def test_new_request_sets_conversation_and_message_ids(self):
        r = _make_request()
        assert r.conversation_id.startswith("conv-")
        assert r.message_id.startswith("msg-")
        assert r.in_reply_to is None

    def test_validate_rejects_missing_field(self):
        r = _make_request()
        r.sender = ""
        with pytest.raises(ValueError, match="sender"):
            r.validate()

    def test_validate_rejects_non_dict_payload(self):
        r = _make_request()
        r.payload = "not a dict"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="payload"):
            r.validate()

    def test_response_ok_preserves_linkage(self):
        req = _make_request()
        res = A2AResponse.ok(
            request=req,
            payload={"files_analyzed": 2},
            payload_schema="ASTReport/v1",
            latency_ms=12.3,
        )
        assert res.conversation_id == req.conversation_id
        assert res.in_reply_to == req.message_id
        assert res.sender == req.recipient      # roles flipped on reply
        assert res.recipient == req.sender
        assert res.status == A2AStatus.OK
        assert res.is_ok

    def test_response_error_carries_structured_error(self):
        req = _make_request()
        res = A2AResponse.error(
            request=req,
            error=A2AError(code="x", message="boom", details={"k": 1}),
            status=A2AStatus.ERROR,
        )
        assert not res.is_ok
        assert res.payload["code"] == "x"
        assert res.payload["message"] == "boom"
        assert res.payload["details"] == {"k": 1}
        assert res.payload_schema == "A2AError/v1"


# ── Dispatcher audit contract ─────────────────────────────────────────────────

class TestDispatcherAudit:
    @pytest.mark.asyncio
    async def test_happy_path_emits_dispatch_and_reply(self):
        audit = _CapturingAudit()
        req = _make_request()

        async def handler(request: A2ARequest) -> A2AResponse:
            return A2AResponse.ok(
                request=request,
                payload={"ok": True},
                payload_schema="ASTReport/v1",
                latency_ms=1.0,
            )

        res = await a2a_call(req, handler, sender_audit=audit)
        assert res.is_ok

        entries = audit._backends[0].entries  # type: ignore[attr-defined]
        event_types = [e.event_type for e in entries]
        assert event_types == ["a2a_dispatch", "a2a_reply"]
        assert entries[0].metadata["conversation_id"] == req.conversation_id
        assert entries[0].metadata["recipient"] == req.recipient
        assert entries[1].metadata["in_reply_to"] == req.message_id
        assert entries[1].metadata["status"] == "ok"

    @pytest.mark.asyncio
    async def test_allowed_recipients_blocks_unknown_callee(self):
        audit = _CapturingAudit()
        req = _make_request(recipient="Rogue-impostor")

        async def handler(request: A2ARequest) -> A2AResponse:  # pragma: no cover
            raise AssertionError("handler should not be called when denied")

        res = await a2a_call(
            req, handler, sender_audit=audit,
            allowed_recipients=["ASTAnalyzer"],
        )
        assert res.status == A2AStatus.DENIED
        assert res.payload["code"] == "recipient_not_allowed"

        entries: list[AuditEntry] = audit._backends[0].entries  # type: ignore[attr-defined]
        # dispatch event recorded as deny, reply event carries denied status
        assert entries[0].event_type == "a2a_dispatch"
        assert entries[0].decision == "deny"
        assert entries[1].event_type == "a2a_reply"
        assert entries[1].metadata["status"] == "denied"

    @pytest.mark.asyncio
    async def test_handler_exception_becomes_error_reply_and_reraises(self):
        audit = _CapturingAudit()
        req = _make_request()

        async def handler(request: A2ARequest) -> A2AResponse:
            raise RuntimeError("tree-sitter exploded")

        with pytest.raises(RuntimeError, match="tree-sitter exploded"):
            await a2a_call(req, handler, sender_audit=audit)

        entries = audit._backends[0].entries  # type: ignore[attr-defined]
        assert [e.event_type for e in entries] == ["a2a_dispatch", "a2a_reply"]
        reply = entries[1]
        assert reply.metadata["status"] == "error"
        # Even though the exception was re-raised, the reply audit row has the
        # structured error — so the ledger reflects the failure permanently.
