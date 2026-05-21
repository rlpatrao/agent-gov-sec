"""
tests/test_reviewer_agent.py — unit tests for Reviewer.

Covers:
  - markdown output parser (recommendation / confidence / blocking issues)
  - content assembly (analysis + contract + tests + source + infra inlined)
  - chunking trips for large source files
  - A2A handler validation (schema mismatch, missing fields)
  - A2A handler happy path with a stub agent
  - optional output_dir disk sink writes review.md

The LLM is always stubbed via _FakeAgent (mirrors test_lambda_analyzer_agent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from a2a.envelope import A2ARequest, A2AStatus
from agents.reviewer_agent import (
    AGENT_TYPE,
    REPORT_SCHEMA,
    REQUEST_SCHEMA,
    ReviewerHandler,
    _collect_review_inputs,
    parse_review_output,
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
        sender="LambdaAnalyzer-test", recipient=f"{AGENT_TYPE}-test",
        run_id="run-1", module_id="mod-1",
        intent="review_module", payload_schema=schema, payload=payload,
    )


# ── Output parser ─────────────────────────────────────────────────────────────

class TestParseReviewOutput:
    def test_labelled_approve(self):
        md = "## Recommendation: APPROVE\n## Confidence Score: 88/100\n"
        rec, conf, blocking = parse_review_output(md)
        assert rec == "APPROVE"
        assert conf == 88
        assert blocking == []

    def test_labelled_blocked_with_blocking_section(self):
        md = """\
## Issues Found
### Blocking
- function_app.py:42 hardcoded credential
- bicep/main.bicep:10 wildcard CORS

### Non-Blocking
- minor

## Recommendation: BLOCKED
## Confidence Score: 35/100
"""
        rec, conf, blocking = parse_review_output(md)
        assert rec == "BLOCKED"
        assert conf == 35
        assert len(blocking) == 2
        assert blocking[0].startswith("function_app.py:42")

    def test_changes_requested_when_label_says_so(self):
        md = "## Recommendation: CHANGES_REQUESTED\n## Confidence Score: 50/100\n"
        rec, _, _ = parse_review_output(md)
        assert rec == "CHANGES_REQUESTED"

    def test_changes_requested_with_space(self):
        md = "## Recommendation: CHANGES REQUESTED\n## Confidence Score: 60/100\n"
        rec, _, _ = parse_review_output(md)
        assert rec == "CHANGES_REQUESTED"

    def test_falls_back_to_tail_scan(self):
        md = "blah blah final verdict APPROVE here"
        rec, _, _ = parse_review_output(md)
        assert rec == "APPROVE"

    def test_default_changes_requested_when_unparseable(self):
        rec, conf, blocking = parse_review_output("nothing useful here")
        assert rec == "CHANGES_REQUESTED"
        assert conf == 0
        assert blocking == []

    def test_confidence_clamped_to_0_100(self):
        md = "## Confidence Score: 250/100\n"
        _, conf, _ = parse_review_output(md)
        assert conf == 100

    def test_not_approve_in_tail_does_not_count(self):
        md = "long doc\n\nfinal: CHANGES_REQUESTED — DO NOT APPROVE this\n"
        rec, _, _ = parse_review_output(md)
        # "DO NOT APPROVE" must not flip the verdict to APPROVE.
        assert rec != "APPROVE"


# ── Content collection ───────────────────────────────────────────────────────

def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestCollectReviewInputs:
    def test_inlines_all_supplied_artifacts(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "function_app.py", "def main(): pass\n")
        bundle = _collect_review_inputs(
            analysis_markdown="## Migration Analysis\nLOW",
            sprint_contract_json='{"unit_checks": []}',
            test_results_markdown="all green",
            failure_reports_json=None,
            migrated_source_dir=str(src),
            infra_dir=None,
        )
        joined = "\n\n".join(bundle.sections)
        assert "Analyzer Output" in joined
        assert "Sprint Contract" in joined
        assert "Test Results" in joined
        assert "Migrated Source Code" in joined
        assert str(src / "function_app.py") in bundle.files_reviewed

    def test_missing_artifacts_are_skipped(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "f.py", "x = 1\n")
        bundle = _collect_review_inputs(
            analysis_markdown=None,
            sprint_contract_json=None,
            test_results_markdown=None,
            failure_reports_json=None,
            migrated_source_dir=str(src),
            infra_dir=None,
        )
        joined = "\n\n".join(bundle.sections)
        assert "Analyzer Output" not in joined
        assert "Sprint Contract" not in joined
        assert "Migrated Source Code" in joined

    def test_infra_dir_inlined_when_present(self, tmp_path: Path):
        src = tmp_path / "src"
        infra = tmp_path / "infra"
        _write(src / "f.py", "pass\n")
        _write(infra / "main.bicep", "resource sa 'Microsoft.Storage/storageAccounts@...'\n")
        bundle = _collect_review_inputs(
            analysis_markdown=None, sprint_contract_json=None,
            test_results_markdown=None, failure_reports_json=None,
            migrated_source_dir=str(src), infra_dir=str(infra),
        )
        joined = "\n\n".join(bundle.sections)
        assert "Infrastructure: main.bicep" in joined
        assert "Microsoft.Storage" in joined

    def test_chunked_marker_for_large_files(self, tmp_path: Path):
        src = tmp_path / "src"
        # 4000 lines triggers needs_chunking (threshold 3000)
        big_body = "\n".join([f"def f_{i}(): pass" for i in range(4000)])
        _write(src / "big.py", big_body)
        bundle = _collect_review_inputs(
            analysis_markdown=None, sprint_contract_json=None,
            test_results_markdown=None, failure_reports_json=None,
            migrated_source_dir=str(src), infra_dir=None,
        )
        assert str(src / "big.py") in bundle.files_chunked


# ── A2A handler ──────────────────────────────────────────────────────────────

class TestHandlerValidation:
    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_error(self):
        h = ReviewerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({}, schema="WrongSchema/v1"))
        assert not resp.is_ok
        assert resp.status == A2AStatus.ERROR
        assert resp.payload["code"] == "schema_mismatch"

    @pytest.mark.asyncio
    async def test_missing_module_returns_error(self):
        h = ReviewerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({"language": "python", "migrated_source_dir": "/tmp"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"

    @pytest.mark.asyncio
    async def test_missing_source_dir_returns_error(self):
        h = ReviewerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({"module": "m", "language": "python"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"


class TestHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_returns_typed_review_report(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "function_app.py", "def main(): return 1\n")

        agent = _FakeAgent(reply_text=(
            "# Code Review: orders\n\n"
            "## Confidence Score: 82/100\n\n"
            "## Issues Found\n"
            "### Blocking\n\n"
            "### Non-Blocking\n- function_app.py:1 — type hint missing\n\n"
            "## Recommendation: APPROVE\n"
        ))
        handler = ReviewerHandler(agent=agent, nhi_id="local-reviewer-nhi")

        resp = await handler.handle(_request({
            "module": "orders", "language": "python",
            "migrated_source_dir": str(src),
            "analysis_markdown": "## Migration Analysis\n- Complexity: LOW\n",
        }))

        assert resp.is_ok
        assert resp.payload_schema == REPORT_SCHEMA
        body = resp.payload
        assert body["module"] == "orders"
        assert body["recommendation"] == "APPROVE"
        assert body["confidence"] == 82
        assert body["blocking_issues"] == []
        assert str(src / "function_app.py") in body["files_reviewed"]

        # Per-call governance headers must reach the agent.
        opts = agent.captured_options[0]
        assert opts["extra_headers"]["x-galaxy-run-id"] == "run-1"
        assert opts["extra_headers"]["x-module-id"] == "mod-1"

    @pytest.mark.asyncio
    async def test_blocking_issues_extracted_into_typed_field(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "f.py", "x=1\n")
        agent = _FakeAgent(reply_text=(
            "## Issues Found\n"
            "### Blocking\n"
            "- f.py:5 hardcoded password\n"
            "- f.py:10 sql injection\n\n"
            "### Non-Blocking\n- nit\n\n"
            "## Recommendation: BLOCKED\n"
            "## Confidence Score: 20/100\n"
        ))
        handler = ReviewerHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python", "migrated_source_dir": str(src),
        }))
        assert resp.payload["recommendation"] == "BLOCKED"
        assert resp.payload["blocking_issues"] == [
            "f.py:5 hardcoded password",
            "f.py:10 sql injection",
        ]

    @pytest.mark.asyncio
    async def test_output_dir_writes_review_md(self, tmp_path: Path):
        src = tmp_path / "src"
        out_root = tmp_path / "out"
        _write(src / "f.py", "x=1\n")
        agent = _FakeAgent(reply_text="# Code Review: m\n## Recommendation: APPROVE\n## Confidence Score: 90/100\n")
        handler = ReviewerHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python",
            "migrated_source_dir": str(src),
            "output_dir": str(out_root),
        }))
        assert resp.is_ok
        written = out_root / "m" / "review.md"
        assert written.is_file()
        assert "Code Review: m" in written.read_text(encoding="utf-8")
