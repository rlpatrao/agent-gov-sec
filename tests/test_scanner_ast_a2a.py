"""
End-to-end A2A tests covering Scanner → ASTAnalyzer handoff.

Validates the demo goals:
  * structured communication — Scanner builds an A2ARequest, not a raw call
  * governance/audit — dispatch + reply land in the sender's audit log
  * traceability — ASTReport is merged back into ScannerOutput with the
    conversation_id so the ledger row and the final output can be joined
  * policy-level denial — dispatching to an unknown recipient is blocked
    even if the handler would otherwise succeed

The tests stub the LLM side (ASTAgentHandler.handle is replaced by a pure
handler) and the ASTAgentHandler path itself (which requires real tree-sitter
+ Azure OpenAI). Tree-sitter is real — the extractor test module already
covers its correctness.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.audit_logger import GovernanceAuditLogger, InMemoryBackend

from a2a.envelope import A2ARequest, A2AResponse, A2AStatus
from agents.scanner_agent import ScannerOutput, dispatch_ast_analysis


@pytest.fixture
def sender_audit() -> GovernanceAuditLogger:
    audit = GovernanceAuditLogger()
    audit.add_backend(InMemoryBackend())
    return audit


def _scanner_output(tmp_path: Path) -> ScannerOutput:
    return ScannerOutput(
        module_id="mod-1",
        language="python",
        file_inventory=["src/main.py", "src/util.py"],
        entry_points=["src/main.py"],
        external_dependencies=[],
        dead_files=[],
        raw_summary="demo",
    )


class TestScannerAstDispatch:
    @pytest.mark.asyncio
    async def test_happy_path_merges_report_into_scanner_output(self, tmp_path, sender_audit):
        out = _scanner_output(tmp_path)
        file_map = {"files": ["src/main.py", "src/util.py"], "detected_language": "python"}
        captured_requests: list[A2ARequest] = []

        async def fake_handler(request: A2ARequest) -> A2AResponse:
            captured_requests.append(request)
            return A2AResponse.ok(
                request=request,
                payload={"architecture_summary": "tiny fastapi service", "risks": []},
                payload_schema="ASTReport/v1",
                latency_ms=5.0,
            )

        response = await dispatch_ast_analysis(
            sender_agent_id="Scanner-local-scanner-nhi",
            recipient_agent_id="ASTAnalyzer-local-ast-nhi",
            run_id="run-xyz",
            module_id="mod-1",
            repo_path=str(tmp_path),
            file_map=file_map,
            scanner_output=out,
            audit=sender_audit,
            handler=fake_handler,
        )

        assert response.is_ok
        assert out.ast_report is not None
        assert out.ast_report["architecture_summary"] == "tiny fastapi service"
        assert out.ast_conversation_id == response.conversation_id

        # Envelope provenance
        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.sender == "Scanner-local-scanner-nhi"
        assert req.recipient == "ASTAnalyzer-local-ast-nhi"
        assert req.payload_schema == "ASTRequest/v1"
        assert req.payload["repo_root"] == str(tmp_path)
        # entry point should be first in the file list (prioritised)
        assert req.payload["files"][0] == "src/main.py"

        # Sender audit has exactly two entries for this hop
        entries = sender_audit._backends[0].entries    # type: ignore[attr-defined]
        assert [e.event_type for e in entries] == ["a2a_dispatch", "a2a_reply"]
        assert entries[0].metadata["conversation_id"] == req.conversation_id
        assert entries[1].metadata["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_recipient_is_denied_by_allow_list(self, tmp_path, sender_audit):
        out = _scanner_output(tmp_path)
        file_map = {"files": [], "detected_language": "python"}

        async def handler(request: A2ARequest) -> A2AResponse:  # pragma: no cover
            raise AssertionError("handler should not run for denied dispatches")

        response = await dispatch_ast_analysis(
            sender_agent_id="Scanner-local-scanner-nhi",
            recipient_agent_id="Rogue-impostor",
            run_id="run-xyz",
            module_id="mod-1",
            repo_path=str(tmp_path),
            file_map=file_map,
            scanner_output=out,
            audit=sender_audit,
            handler=handler,
        )

        assert response.status == A2AStatus.DENIED
        assert out.ast_report is None      # no report merged on deny
        entries = sender_audit._backends[0].entries   # type: ignore[attr-defined]
        assert entries[0].decision == "deny"

    @pytest.mark.asyncio
    async def test_error_response_keeps_audit_trail_but_leaves_output_empty(self, tmp_path, sender_audit):
        out = _scanner_output(tmp_path)
        file_map = {"files": [], "detected_language": "python"}

        async def handler(request: A2ARequest) -> A2AResponse:
            raise RuntimeError("parser blew up")

        with pytest.raises(RuntimeError):
            await dispatch_ast_analysis(
                sender_agent_id="Scanner-local-scanner-nhi",
                recipient_agent_id="ASTAnalyzer-local-ast-nhi",
                run_id="run-xyz",
                module_id="mod-1",
                repo_path=str(tmp_path),
                file_map=file_map,
                scanner_output=out,
                audit=sender_audit,
                handler=handler,
            )

        # Even though the exception re-raised, both audit events were written
        entries = sender_audit._backends[0].entries    # type: ignore[attr-defined]
        assert [e.event_type for e in entries] == ["a2a_dispatch", "a2a_reply"]
        assert entries[1].metadata["status"] == "error"
        assert out.ast_report is None


class TestFilePrioritization:
    def test_entry_points_come_first_and_cap_honoured(self):
        from agents.scanner_agent import MAX_AST_FILES_PER_DISPATCH, _pick_files_for_ast

        out = ScannerOutput(
            module_id="m",
            language="python",
            file_inventory=[f"f{i}.py" for i in range(100)],
            entry_points=["main.py"],
            external_dependencies=[],
            dead_files=[],
            raw_summary="",
        )
        picks = _pick_files_for_ast({"files": []}, out)
        assert picks[0] == "main.py"
        assert len(picks) == MAX_AST_FILES_PER_DISPATCH
