"""
tests/test_security_reviewer_agent.py — unit tests for SecurityReviewer.

Covers:
  - deterministic regex scanner (BLOCK / WARN / INFO; test-file downgrade)
  - markdown findings table formatting
  - recommendation FLOOR rule: regex BLOCK never demoted by LLM
  - A2A handler validation + happy path
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from a2a.envelope import A2ARequest, A2AStatus
from agents._lib.security_scanner import SecurityFinding, scan_directory, scan_file
from agents.security_reviewer_agent import (
    AGENT_TYPE,
    REPORT_SCHEMA,
    REQUEST_SCHEMA,
    SecurityReviewerHandler,
    _combine_recommendation,
    _format_findings_table,
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAgent:
    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.captured_prompts: list[str] = []
        self.captured_options: list[dict] = []

    async def run(self, prompt: str, options: dict[str, Any] | None = None) -> _FakeResponse:
        self.captured_prompts.append(prompt)
        self.captured_options.append(options or {})
        return _FakeResponse(self.reply_text)


def _request(payload: dict, *, schema: str = REQUEST_SCHEMA) -> A2ARequest:
    return A2ARequest.new(
        sender="Reviewer-test", recipient=f"{AGENT_TYPE}-test",
        run_id="run-1", module_id="mod-1",
        intent="security_review_module", payload_schema=schema, payload=payload,
    )


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ── Deterministic scanner ─────────────────────────────────────────────────────

class TestScanFile:
    def test_aws_access_key_blocks(self, tmp_path: Path):
        f = _write(tmp_path / "leak.py", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        results = scan_file(f)
        assert any(r.severity == "BLOCK" and "AWS access key" in r.category for r in results)

    def test_eval_blocks(self, tmp_path: Path):
        f = _write(tmp_path / "evil.py", "x = eval(user_input)\n")
        results = scan_file(f)
        assert any(r.severity == "BLOCK" and "eval" in r.category for r in results)

    def test_debug_true_warns(self, tmp_path: Path):
        f = _write(tmp_path / "settings.py", "DEBUG = True\n")
        results = scan_file(f)
        assert any(r.severity == "WARN" and "Debug" in r.category for r in results)

    def test_findings_in_test_file_downgraded_to_info(self, tmp_path: Path):
        f = _write(tmp_path / "test_leak.py", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        results = scan_file(f)
        # Real file would BLOCK; test file path downgrades to INFO.
        assert all(r.severity == "INFO" for r in results)

    def test_comment_lines_skipped(self, tmp_path: Path):
        f = _write(tmp_path / "c.py", '# example: AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        assert scan_file(f) == []

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert scan_file(tmp_path / "nope.py") == []


class TestScanDirectory:
    def test_walks_recursively_and_skips_pycache(self, tmp_path: Path):
        _write(tmp_path / "a.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        _write(tmp_path / "sub" / "b.py", "x = eval(z)\n")
        _write(tmp_path / "__pycache__" / "c.cpython.pyc", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        results = scan_directory(tmp_path)
        files = {r.file for r in results}
        assert any(str(tmp_path / "a.py") == f for f in files)
        assert any(str(tmp_path / "sub" / "b.py") == f for f in files)
        assert not any("__pycache__" in f for f in files)

    def test_returns_empty_when_dir_missing(self, tmp_path: Path):
        assert scan_directory(tmp_path / "ghost") == []


# ── Findings table ────────────────────────────────────────────────────────────

class TestFormatFindingsTable:
    def test_no_findings(self):
        assert _format_findings_table([]) == "No automated findings."

    def test_table_has_header_and_rows(self):
        f1 = SecurityFinding(file="f.py", line=3, category="AWS access key",
                             severity="BLOCK", description="Pattern matched: AKIA...")
        out = _format_findings_table([f1])
        assert "| File | Line | Category | Severity | Pattern Matched |" in out
        assert "| f.py | 3 | AWS access key | BLOCK | Pattern matched: AKIA... |" in out

    def test_pipes_in_snippet_are_escaped(self):
        f = SecurityFinding(file="x|y.py", line=1, category="cat",
                            severity="WARN", description="a|b|c")
        out = _format_findings_table([f])
        assert r"x\|y.py" in out
        assert r"a\|b\|c" in out


# ── Recommendation floor ──────────────────────────────────────────────────────

class TestCombineRecommendation:
    def test_block_finding_floors_llm_approve(self):
        block = [SecurityFinding(file="f.py", line=1, category="AWS access key",
                                 severity="BLOCK", description="Pattern matched: AKIA...")]
        assert _combine_recommendation(block, "APPROVE") == "BLOCKED"

    def test_warn_only_does_not_floor(self):
        warn = [SecurityFinding(file="f.py", line=1, category="Debug mode enabled",
                                severity="WARN", description="...")]
        assert _combine_recommendation(warn, "APPROVE") == "APPROVE"

    def test_no_findings_passes_llm_verdict_through(self):
        assert _combine_recommendation([], "BLOCKED") == "BLOCKED"
        assert _combine_recommendation([], "APPROVE") == "APPROVE"

    def test_llm_can_promote_to_blocked_even_without_regex_block(self):
        # LLM caught a logic bug regex didn't — must be honoured.
        warn = [SecurityFinding(file="f.py", line=1, category="cat",
                                severity="WARN", description="...")]
        assert _combine_recommendation(warn, "BLOCKED") == "BLOCKED"


# ── A2A handler ──────────────────────────────────────────────────────────────

class TestHandlerValidation:
    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_error(self):
        h = SecurityReviewerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({}, schema="WrongSchema/v1"))
        assert not resp.is_ok
        assert resp.status == A2AStatus.ERROR
        assert resp.payload["code"] == "schema_mismatch"

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_error(self):
        h = SecurityReviewerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({"module": "m"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"


class TestHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_returns_typed_report_with_counts_and_floor(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "leak.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')   # BLOCK
        _write(src / "warn.py", "DEBUG = True\n")                  # WARN

        agent = _FakeAgent(reply_text=(
            "# Security Review: orders\n\n"
            "## Recommendation: APPROVE\n"   # LLM tries to APPROVE; floor must override
            "## Confidence Score: 70/100\n"
        ))
        handler = SecurityReviewerHandler(agent=agent)

        resp = await handler.handle(_request({
            "module": "orders", "language": "python",
            "migrated_source_dir": str(src),
        }))

        assert resp.is_ok
        assert resp.payload_schema == REPORT_SCHEMA
        body = resp.payload
        assert body["module"] == "orders"
        # Floor rule: deterministic BLOCK demotes the LLM's APPROVE to BLOCKED.
        assert body["recommendation"] == "BLOCKED"
        assert body["block_count"] >= 1
        assert body["warn_count"] >= 1
        assert any(f["severity"] == "BLOCK" for f in body["automated_findings"])

        # Per-call headers reach the agent.
        opts = agent.captured_options[0]
        assert opts["extra_headers"]["x-galaxy-run-id"] == "run-1"
        assert opts["extra_headers"]["x-module-id"] == "mod-1"

    @pytest.mark.asyncio
    async def test_clean_source_passes_through_llm_approve(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "clean.py", "def main():\n    return 'hello'\n")
        agent = _FakeAgent(reply_text=(
            "# Security Review\n## Recommendation: APPROVE\n## Confidence Score: 95/100\n"
        ))
        handler = SecurityReviewerHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python", "migrated_source_dir": str(src),
        }))
        assert resp.payload["recommendation"] == "APPROVE"
        assert resp.payload["block_count"] == 0

    @pytest.mark.asyncio
    async def test_output_dir_writes_security_review_md(self, tmp_path: Path):
        src = tmp_path / "src"
        out_root = tmp_path / "out"
        _write(src / "clean.py", "def main(): pass\n")
        agent = _FakeAgent(reply_text="# Security Review\n## Recommendation: APPROVE\n")
        handler = SecurityReviewerHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python",
            "migrated_source_dir": str(src),
            "output_dir": str(out_root),
        }))
        assert resp.is_ok
        written = out_root / "m" / "security-review.md"
        assert written.is_file()
        assert "Security Review" in written.read_text(encoding="utf-8")
