"""
tests/test_lambda_analyzer_agent.py — unit tests for LambdaAnalyzer.

Covers:
  - deterministic complexity scoring + aggregation across files
  - source assembly with chunking + read-skip behaviour
  - A2A handler validation (schema mismatch, missing fields)
  - A2A handler happy path with a stub agent (no live LLM)
  - optional output_dir sink writes analysis.md to disk

The LLM is always stubbed via _FakeAgent. A live-LLM probe lives in
docs/user-guide.md §9.x (policy-probe pattern) and is run manually.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from a2a.envelope import A2ARequest, A2AStatus
from agents._lib.complexity_scorer import score_complexity
from agents.lambda_analyzer_agent import (
    AGENT_TYPE,
    REPORT_SCHEMA,
    REQUEST_SCHEMA,
    LambdaAnalyzerHandler,
    _aggregate_complexity,
    _build_user_prompt,
    _collect_source,
)


# ── Stub agent ────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAgent:
    """Minimal stand-in for a MAF Agent. Records the prompts it received."""

    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.captured_prompts: list[str] = []
        self.captured_options: list[dict] = []

    async def run(self, prompt: str, options: dict[str, Any] | None = None) -> _FakeResponse:
        self.captured_prompts.append(prompt)
        self.captured_options.append(options or {})
        return _FakeResponse(self.reply_text)


# ── Source fixtures ───────────────────────────────────────────────────────────

LAMBDA_PY_SIMPLE = """\
import boto3

s3 = boto3.client('s3')

def handler(event, context):
    s3.get_object(Bucket='b', Key=event['key'])
    return {"statusCode": 200}
"""

LAMBDA_PY_COMPLEX = """\
import boto3
import threading

sfn = boto3.client('stepfunctions')
ev = boto3.client('events')
sqs = boto3.client('sqs')
sns = boto3.client('sns')

class OrderHandler:
    def handler(self, event, context):
        sfn.start_execution(stateMachineArn='arn:...', input='{}')
        ev.put_events(Entries=[])
        sqs.send_message(QueueUrl='q', MessageBody='m')
        sns.publish(TopicArn='t', Message='m')
"""


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


# ── Deterministic helpers ─────────────────────────────────────────────────────

class TestComplexityScorer:
    def test_simple_module_scores_low(self, tmp_path: Path):
        f = _write(tmp_path, "simple.py", LAMBDA_PY_SIMPLE)
        result = score_complexity(str(f), "python")
        assert result.level == "LOW"
        assert result.score < 5

    def test_complex_module_scores_high(self, tmp_path: Path):
        f = _write(tmp_path, "complex.py", LAMBDA_PY_COMPLEX)
        result = score_complexity(str(f), "python")
        # Step Functions alone is +4; with 5 boto clients + concurrency we expect HIGH.
        assert result.level == "HIGH"
        assert result.score >= 15

    def test_unreadable_file_returns_unknown(self, tmp_path: Path):
        result = score_complexity(str(tmp_path / "missing.py"), "python")
        assert result.level == "UNKNOWN"
        assert result.score == 0

    def test_aggregate_sums_across_files(self, tmp_path: Path):
        a = _write(tmp_path, "a.py", LAMBDA_PY_SIMPLE)
        b = _write(tmp_path, "b.py", LAMBDA_PY_COMPLEX)
        agg = _aggregate_complexity([str(a), str(b)], "python")
        assert agg.level == "HIGH"
        # Aggregate must be >= the higher of the two individual scores.
        assert agg.score >= score_complexity(str(b), "python").score

    def test_aggregate_empty_returns_unknown(self):
        agg = _aggregate_complexity([], "python")
        assert agg.level == "UNKNOWN"


# ── Source assembly ───────────────────────────────────────────────────────────

class TestCollectSource:
    def test_explicit_source_paths_wins_over_dir(self, tmp_path: Path):
        a = _write(tmp_path, "a.py", LAMBDA_PY_SIMPLE)
        _write(tmp_path, "ignored.py", "# should not appear")
        bundle = _collect_source(
            source_paths=[str(a)],
            source_dir=str(tmp_path),
            context_paths=[],
        )
        assert str(a) in bundle.files_included
        assert "ignored.py" not in bundle.listing

    def test_falls_back_to_rglob_when_paths_empty(self, tmp_path: Path):
        _write(tmp_path, "a.py", LAMBDA_PY_SIMPLE)
        sub = tmp_path / "sub"
        sub.mkdir()
        _write(sub, "b.py", LAMBDA_PY_SIMPLE)
        bundle = _collect_source(
            source_paths=[],
            source_dir=str(tmp_path),
            context_paths=[],
        )
        assert len(bundle.files_included) == 2

    def test_unreadable_path_is_skipped_not_fatal(self, tmp_path: Path):
        good = _write(tmp_path, "good.py", LAMBDA_PY_SIMPLE)
        missing = tmp_path / "ghost.py"
        bundle = _collect_source(
            source_paths=[str(good), str(missing)],
            source_dir=None,
            context_paths=[],
        )
        assert str(good) in bundle.files_included
        assert str(missing) in bundle.files_skipped

    def test_context_paths_appear_under_read_only_header(self, tmp_path: Path):
        src = _write(tmp_path, "src.py", LAMBDA_PY_SIMPLE)
        ctx = _write(tmp_path, "shared_lib.py", "def helper(): pass")
        bundle = _collect_source(
            source_paths=[str(src)],
            source_dir=None,
            context_paths=[str(ctx)],
        )
        assert "anti-corruption boundary" in bundle.listing
        assert "shared_lib.py" in bundle.listing


class TestPromptAssembly:
    def test_prompt_carries_complexity_block_and_source(self, tmp_path: Path):
        f = _write(tmp_path, "x.py", LAMBDA_PY_SIMPLE)
        bundle = _collect_source(source_paths=[str(f)], source_dir=None, context_paths=[])
        complexity = _aggregate_complexity([str(f)], "python")
        prompt = _build_user_prompt(
            module="ordersvc", language="python",
            complexity=complexity, source_listing=bundle.listing,
        )
        assert "ordersvc" in prompt
        assert "Pre-computed Complexity Score" in prompt
        assert f"Overall score: {complexity.score}" in prompt
        # Source listing must be inlined verbatim — the LLM must see the code.
        assert "boto3.client('s3')" in prompt


# ── A2A handler ───────────────────────────────────────────────────────────────

def _request(payload: dict, *, schema: str = REQUEST_SCHEMA) -> A2ARequest:
    return A2ARequest.new(
        sender="Scanner-test", recipient=f"{AGENT_TYPE}-test",
        run_id="run-1", module_id="mod-1",
        intent="analyze_module", payload_schema=schema, payload=payload,
    )


class TestHandlerValidation:
    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_error(self):
        h = LambdaAnalyzerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({}, schema="WrongSchema/v1"))
        assert not resp.is_ok
        assert resp.status == A2AStatus.ERROR
        assert resp.payload_schema == "A2AError/v1"
        assert resp.payload["code"] == "schema_mismatch"

    @pytest.mark.asyncio
    async def test_missing_module_returns_error(self):
        h = LambdaAnalyzerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({"language": "python", "source_dir": "/tmp"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"

    @pytest.mark.asyncio
    async def test_missing_source_returns_error(self):
        h = LambdaAnalyzerHandler(agent=_FakeAgent("..."))
        resp = await h.handle(_request({"module": "m", "language": "python"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"


class TestHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_returns_analysis_report_with_complexity(self, tmp_path: Path):
        src = _write(tmp_path, "h.py", LAMBDA_PY_SIMPLE)
        agent = _FakeAgent(reply_text="# Migration Analysis: orders\n\n## Summary\n- Language: python\n")
        handler = LambdaAnalyzerHandler(agent=agent, nhi_id="local-lambdaanalyzer-nhi")

        resp = await handler.handle(_request({
            "module": "orders", "language": "python",
            "source_paths": [str(src)],
        }))

        assert resp.is_ok
        assert resp.payload_schema == REPORT_SCHEMA
        body = resp.payload
        assert body["module"] == "orders"
        assert body["complexity_level"] == "LOW"
        assert str(src) in body["files_included"]
        assert "Migration Analysis" in body["analysis_markdown"]

        # Per-call governance headers must reach the agent.
        opts = agent.captured_options[0]
        assert opts["extra_headers"]["x-galaxy-run-id"] == "run-1"
        assert opts["extra_headers"]["x-module-id"] == "mod-1"

    @pytest.mark.asyncio
    async def test_output_dir_writes_analysis_md(self, tmp_path: Path):
        src = _write(tmp_path, "h.py", LAMBDA_PY_SIMPLE)
        out_root = tmp_path / "out"
        agent = _FakeAgent(reply_text="# Migration Analysis: orders\n")
        handler = LambdaAnalyzerHandler(agent=agent)

        resp = await handler.handle(_request({
            "module": "orders", "language": "python",
            "source_paths": [str(src)],
            "output_dir": str(out_root),
        }))

        assert resp.is_ok
        written = out_root / "orders" / "analysis.md"
        assert written.is_file()
        assert "Migration Analysis" in written.read_text(encoding="utf-8")
