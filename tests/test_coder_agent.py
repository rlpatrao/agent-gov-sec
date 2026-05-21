"""
tests/test_coder_agent.py — unit tests for Coder + sandboxed file tools.

Covers:
  - make_write_file: success, sandbox rejection, parent-dir auto-create,
    OS errors propagate as ERROR strings (the LLM reads them and adapts).
  - make_apply_patch: all-or-nothing batching, count mismatch, sandbox
    rejection of any single path aborts the whole batch (no partial writes).
  - Coder source assembly + prompt build.
  - Snapshot/diff logic that lets the handler verify what the LLM wrote.
  - A2A handler validation (schema mismatch, missing fields).
  - A2A handler happy path with a stub agent that writes via the tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from a2a.envelope import A2ARequest, A2AStatus
from agents._lib.file_tools import make_apply_patch, make_write_file
from agents.coder_agent import (
    AGENT_TYPE,
    REPORT_SCHEMA,
    REQUEST_SCHEMA,
    CoderHandler,
    _collect_source_listing,
    _diff_snapshots,
    _snapshot,
)


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAgent:
    """Stub agent that ALSO writes files via tools to simulate the real flow.

    The handler builds tools at the closure layer and passes them to the real
    Agent — but a fake agent never invokes them. To exercise the snapshot/diff
    path, this stub accepts a callback to do file I/O before returning.
    """

    def __init__(self, reply_text: str, side_effect=None) -> None:
        self.reply_text = reply_text
        self.side_effect = side_effect
        self.captured_prompts: list[str] = []
        self.captured_options: list[dict] = []

    async def run(self, prompt: str, options: dict[str, Any] | None = None) -> _FakeResponse:
        self.captured_prompts.append(prompt)
        self.captured_options.append(options or {})
        if self.side_effect is not None:
            self.side_effect(prompt)
        return _FakeResponse(self.reply_text)


def _request(payload: dict, *, schema: str = REQUEST_SCHEMA) -> A2ARequest:
    return A2ARequest.new(
        sender="LambdaAnalyzer-test", recipient=f"{AGENT_TYPE}-test",
        run_id="run-1", module_id="mod-1",
        intent="migrate_module", payload_schema=schema, payload=payload,
    )


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ── make_write_file ──────────────────────────────────────────────────────────

class TestMakeWriteFile:
    def test_writes_inside_sandbox(self, tmp_path: Path):
        wf = make_write_file([tmp_path])
        out = wf.func(str(tmp_path / "sub" / "f.py"), "x = 1\n")
        assert "Written" in out
        assert (tmp_path / "sub" / "f.py").read_text() == "x = 1\n"

    def test_refuses_path_outside_sandbox(self, tmp_path: Path):
        wf = make_write_file([tmp_path])
        outside = tmp_path.parent / "elsewhere" / "leak.py"
        out = wf.func(str(outside), "secret = 1\n")
        assert out.startswith("ERROR: write outside sandbox")
        assert not outside.exists()

    def test_refuses_traversal_via_dotdot(self, tmp_path: Path):
        wf = make_write_file([tmp_path])
        # Resolve normalises ../ — landing outside tmp_path → reject.
        traversal = tmp_path / "sub" / ".." / ".." / "leak.py"
        out = wf.func(str(traversal), "x")
        assert out.startswith("ERROR: write outside sandbox")

    def test_two_roots_both_allowed(self, tmp_path: Path):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        wf = make_write_file([a, b])
        assert "Written" in wf.func(str(a / "x.py"), "1\n")
        assert "Written" in wf.func(str(b / "y.py"), "2\n")

    def test_function_tool_name_is_write_file(self, tmp_path: Path):
        wf = make_write_file([tmp_path])
        # FunctionTool.name must match governance.allowed_tools in YAML.
        assert wf.name == "write_file"


# ── make_apply_patch ─────────────────────────────────────────────────────────

class TestMakeApplyPatch:
    def test_atomic_batch_all_or_nothing(self, tmp_path: Path):
        a = _write(tmp_path / "a.py", "old1\n")
        b = _write(tmp_path / "b.py", "old2\n")
        ap = make_apply_patch([tmp_path])
        # Second edit's count is wrong (expected 2, only 1 match) — whole batch aborts.
        out = ap.func([
            {"file": str(a), "old_string": "old1", "new_string": "new1"},
            {"file": str(b), "old_string": "old2", "new_string": "new2", "expected_count": 2},
        ])
        assert out.startswith("ERROR")
        assert a.read_text() == "old1\n"   # unchanged — atomic property
        assert b.read_text() == "old2\n"

    def test_happy_batch(self, tmp_path: Path):
        a = _write(tmp_path / "a.py", "FOO + FOO\n")
        ap = make_apply_patch([tmp_path])
        out = ap.func([
            {"file": str(a), "old_string": "FOO", "new_string": "BAR", "expected_count": 2},
        ])
        assert "applied 1 edit" in out
        assert a.read_text() == "BAR + BAR\n"

    def test_path_outside_sandbox_aborts_batch(self, tmp_path: Path):
        a = _write(tmp_path / "a.py", "old\n")
        outside = tmp_path.parent / "elsewhere"; outside.mkdir(exist_ok=True)
        b_outside = _write(outside / "b.py", "old\n")
        ap = make_apply_patch([tmp_path])
        out = ap.func([
            {"file": str(a),         "old_string": "old", "new_string": "new"},
            {"file": str(b_outside), "old_string": "old", "new_string": "new"},
        ])
        assert "outside sandbox" in out
        assert a.read_text() == "old\n"
        assert b_outside.read_text() == "old\n"

    def test_missing_file_fails_batch(self, tmp_path: Path):
        a = _write(tmp_path / "a.py", "old\n")
        ap = make_apply_patch([tmp_path])
        out = ap.func([
            {"file": str(a), "old_string": "old", "new_string": "new"},
            {"file": str(tmp_path / "ghost.py"), "old_string": "x", "new_string": "y"},
        ])
        assert "file not found" in out
        assert a.read_text() == "old\n"

    def test_function_tool_name_is_apply_patch(self, tmp_path: Path):
        ap = make_apply_patch([tmp_path])
        assert ap.name == "apply_patch"


# ── Source assembly ──────────────────────────────────────────────────────────

class TestCollectSourceListing:
    def test_explicit_paths_wins_over_dir(self, tmp_path: Path):
        a = _write(tmp_path / "a.py", "import boto3\n")
        _write(tmp_path / "ignored.py", "# should not appear")
        listing = _collect_source_listing(
            source_paths=[str(a)], source_dir=str(tmp_path), context_paths=[],
        )
        assert "import boto3" in listing
        assert "ignored.py" not in listing

    def test_context_paths_under_anti_corruption_header(self, tmp_path: Path):
        src = _write(tmp_path / "src.py", "x = 1\n")
        ctx = _write(tmp_path / "shared_lib.py", "def helper(): pass\n")
        listing = _collect_source_listing(
            source_paths=[str(src)], source_dir=None, context_paths=[str(ctx)],
        )
        assert "anti-corruption boundary" in listing
        assert "shared_lib.py" in listing


# ── Snapshot + diff ──────────────────────────────────────────────────────────

class TestSnapshotDiff:
    def test_detects_new_files(self, tmp_path: Path):
        before = _snapshot(tmp_path)
        _write(tmp_path / "new.py", "x\n")
        after = _snapshot(tmp_path)
        written, modified = _diff_snapshots(before, after)
        assert any("new.py" in p for p in written)
        assert modified == []

    def test_detects_modified_files(self, tmp_path: Path):
        existing = _write(tmp_path / "old.py", "v1\n")
        before = _snapshot(tmp_path)
        # Bump mtime by writing fresh content; allow filesystem mtime to advance
        import os, time
        time.sleep(0.01)
        os.utime(existing, (existing.stat().st_atime, existing.stat().st_mtime + 1))
        existing.write_text("v2\n")
        after = _snapshot(tmp_path)
        written, modified = _diff_snapshots(before, after)
        assert any("old.py" in p for p in modified)
        assert written == []

    def test_empty_when_nothing_changes(self, tmp_path: Path):
        _write(tmp_path / "a.py", "1\n")
        before = _snapshot(tmp_path)
        after = _snapshot(tmp_path)
        assert _diff_snapshots(before, after) == ([], [])


# ── A2A handler ──────────────────────────────────────────────────────────────

class TestHandlerValidation:
    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_error(self):
        h = CoderHandler(agent=_FakeAgent(""))
        resp = await h.handle(_request({}, schema="WrongSchema/v1"))
        assert not resp.is_ok
        assert resp.status == A2AStatus.ERROR
        assert resp.payload["code"] == "schema_mismatch"

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_error(self):
        h = CoderHandler(agent=_FakeAgent(""))
        resp = await h.handle(_request({"module": "m"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"


class TestHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_diff_picks_up_files_written_by_agent(self, tmp_path: Path):
        # Stub agent writes one file directly (simulating what the tool path
        # would do at runtime — we're testing the handler's diff logic, not
        # the LLM-tool integration).
        output_root = tmp_path / "out"
        module = "orders"

        def stub_writes(_prompt: str) -> None:
            target = output_root / module / "function_app.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def main(): return 1\n", encoding="utf-8")

        agent = _FakeAgent(reply_text="Wrote function_app.py", side_effect=stub_writes)
        handler = CoderHandler(agent=agent, nhi_id="local-coder-nhi")

        resp = await handler.handle(_request({
            "module": module, "language": "python", "attempt": 1,
            "output_root": str(output_root),
            "source_paths": [],
            "source_dir": None,
        }))

        assert resp.is_ok
        assert resp.payload_schema == REPORT_SCHEMA
        body = resp.payload
        assert body["module"] == module
        assert body["attempt"] == 1
        assert any("function_app.py" in f for f in body["files_written"])
        assert "Wrote function_app.py" in body["summary_markdown"]

        # Per-call governance headers reach the agent.
        opts = agent.captured_options[0]
        assert opts["extra_headers"]["x-galaxy-run-id"] == "run-1"
        assert opts["extra_headers"]["x-module-id"] == "mod-1"

    @pytest.mark.asyncio
    async def test_attempt_and_failure_context_reach_prompt(self, tmp_path: Path):
        agent = _FakeAgent(reply_text="retry summary")
        handler = CoderHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python", "attempt": 3,
            "output_root": str(tmp_path / "out"),
            "previous_failures_json": '{"failures": [{"failure_id": "F001"}]}',
        }))
        assert resp.payload["attempt"] == 3
        # Self-healing context must be inlined into the prompt under the
        # "Previous Failure Report" heading; the LLM sees it on retries.
        prompt = agent.captured_prompts[0]
        assert "Previous Failure Report" in prompt
        assert "F001" in prompt
        assert "attempt 3/3" in prompt

    @pytest.mark.asyncio
    async def test_no_files_written_returns_empty_lists(self, tmp_path: Path):
        agent = _FakeAgent(reply_text="(nothing to do)")
        handler = CoderHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python", "attempt": 1,
            "output_root": str(tmp_path / "out"),
        }))
        assert resp.is_ok
        assert resp.payload["files_written"] == []
        assert resp.payload["files_modified"] == []
        assert resp.payload["bicep_path"] is None
