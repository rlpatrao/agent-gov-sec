"""
tests/test_analyzer_agent.py — unit tests for the generic Analyzer agent.

Covers:
  - Schema mismatch returns A2A error
  - Missing required fields returns A2A error
  - mapping_not_found error when codebase_type has no mapping entry
  - Happy path: explicit codebase_type → AnalysisReport/v1 payload
  - Auto-classify path: no codebase_type → classifier runs on source_dir
  - target_services are populated from the mapping
  - analysis_markdown reaches the prompt (LLM stub returns it)
  - output_dir sink writes analysis.md
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# The handler logic here is agnostic and runs against a fake agent, but
# importing payload_agents.analyzer_agent pulls the MAF chat client at module
# load. Skip cleanly until that import is deferred (see tests/README.md) or MAF
# is installed via '.[azure]'.
pytest.importorskip(
    "agent_framework",
    reason="payload_agents.analyzer_agent imports MAF at module load — install '.[azure]'",
)

from a2a.envelope import A2ARequest
from payload_agents.analyzer_agent import (
    AGENT_TYPE,
    REPORT_SCHEMA,
    REQUEST_SCHEMA,
    AnalyzerHandler,
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAgent:
    def __init__(self, reply: str = "# Analysis\n## Overall Verdict: PASS\n") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def run(self, prompt: str, options: dict[str, Any] | None = None) -> _FakeResponse:
        self.prompts.append(prompt)
        return _FakeResponse(self.reply)


def _req(payload: dict, schema: str = REQUEST_SCHEMA) -> A2ARequest:
    return A2ARequest.new(
        sender="Orchestrator-test", recipient=f"{AGENT_TYPE}-test",
        run_id="run-1", module_id="mod-1",
        intent="analyze_module", payload_schema=schema, payload=payload,
    )


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _make_handler(reply: str = "# Analysis\n") -> tuple[AnalyzerHandler, _FakeAgent]:
    agent = _FakeAgent(reply=reply)
    handler = AnalyzerHandler(agent=agent)
    return handler, agent


# ── Validation ────────────────────────────────────────────────────────────────

class TestHandlerValidation:
    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_error(self):
        h, _ = _make_handler()
        resp = await h.handle(_req({}, schema="WrongSchema/v1"))
        assert not resp.is_ok
        assert resp.payload["code"] == "schema_mismatch"

    @pytest.mark.asyncio
    async def test_missing_module_returns_error(self):
        h, _ = _make_handler()
        resp = await h.handle(_req({"source_dir": "/tmp"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"

    @pytest.mark.asyncio
    async def test_missing_source_returns_error(self, tmp_path: Path):
        h, _ = _make_handler()
        resp = await h.handle(_req({"module": "m"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"

    @pytest.mark.asyncio
    async def test_unknown_codebase_type_returns_mapping_not_found(self, tmp_path: Path):
        _write(tmp_path / "app.py", "x = 1\n")
        h, _ = _make_handler()
        resp = await h.handle(_req({
            "module": "m",
            "source_dir": str(tmp_path),
            "codebase_type": "cobol_mainframe",  # not in mapping
        }))
        assert not resp.is_ok
        assert resp.payload["code"] == "mapping_not_found"
        assert "cobol_mainframe" in resp.payload["message"]

    @pytest.mark.asyncio
    async def test_unclassifiable_source_dir_returns_mapping_not_found(self, tmp_path: Path):
        # Source dir with no AWS signals → classifier returns None → error
        _write(tmp_path / "hello.py", "print('hello')\n")
        h, _ = _make_handler()
        resp = await h.handle(_req({
            "module": "m",
            "source_dir": str(tmp_path),
            # no codebase_type → auto-classify
        }))
        assert not resp.is_ok
        assert resp.payload["code"] == "mapping_not_found"


# ── Happy path ────────────────────────────────────────────────────────────────

class TestHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_explicit_codebase_type_returns_analysis_report(self, tmp_path: Path):
        _write(tmp_path / "handler.py", "import boto3\ndef lambda_handler(e, c): pass\n")
        h, agent = _make_handler(reply="# Migration Analysis: orders\n## Summary\n- Complexity: LOW\n")
        resp = await h.handle(_req({
            "module": "orders",
            "language": "python",
            "source_dir": str(tmp_path),
            "codebase_type": "python_serverless",
        }))
        assert resp.is_ok
        assert resp.payload_schema == REPORT_SCHEMA
        body = resp.payload
        assert body["module"] == "orders"
        assert body["codebase_type"] == "python_serverless"
        assert "azure_functions" in body["target_services"]
        assert body["analysis_markdown"].startswith("# Migration Analysis")

    @pytest.mark.asyncio
    async def test_mapping_context_injected_into_prompt(self, tmp_path: Path):
        _write(tmp_path / "handler.py", "import boto3\n")
        h, agent = _make_handler()
        await h.handle(_req({
            "module": "m",
            "source_dir": str(tmp_path),
            "codebase_type": "python_serverless",
        }))
        prompt = agent.prompts[0]
        assert "python_serverless" in prompt
        assert "azure_functions" in prompt
        assert "migration_approach" in prompt.lower() or "migration steps" in prompt.lower()

    @pytest.mark.asyncio
    async def test_auto_classify_python_serverless(self, tmp_path: Path):
        _write(tmp_path / "requirements.txt", "boto3\n")
        _write(tmp_path / "handler.py", "import boto3\ndef lambda_handler(e, c): pass\n")
        h, agent = _make_handler()
        resp = await h.handle(_req({
            "module": "m",
            "source_dir": str(tmp_path),
            # no codebase_type — let classifier run
        }))
        assert resp.is_ok
        assert resp.payload["codebase_type"] == "python_serverless"
        assert resp.payload["classifier_confidence"] > 0

    @pytest.mark.asyncio
    async def test_output_dir_writes_analysis_md(self, tmp_path: Path):
        src = tmp_path / "src"
        out = tmp_path / "out"
        _write(src / "handler.py", "import boto3\n")
        h, _ = _make_handler(reply="# Analysis content\n")
        resp = await h.handle(_req({
            "module": "orders",
            "source_dir": str(src),
            "codebase_type": "python_serverless",
            "output_dir": str(out),
        }))
        assert resp.is_ok
        analysis_file = out / "orders" / "analysis.md"
        assert analysis_file.is_file()
        assert "Analysis content" in analysis_file.read_text()

    @pytest.mark.asyncio
    async def test_typescript_serverless_returns_correct_target_services(self, tmp_path: Path):
        _write(tmp_path / "tsconfig.json", "{}")
        _write(tmp_path / "handler.ts", "import { APIGatewayProxyHandler } from 'aws-lambda';\n")
        h, _ = _make_handler()
        resp = await h.handle(_req({
            "module": "api",
            "language": "typescript",
            "source_dir": str(tmp_path),
            "codebase_type": "typescript_serverless",
        }))
        assert resp.is_ok
        assert "azure_functions" in resp.payload["target_services"]
        assert "cosmos_db_nosql" in resp.payload["target_services"]

    @pytest.mark.asyncio
    async def test_iac_terraform_mapping_works(self, tmp_path: Path):
        _write(tmp_path / "main.tf", 'provider "aws" {}\nresource "aws_s3_bucket" "b" {}\n')
        h, _ = _make_handler()
        resp = await h.handle(_req({
            "module": "infra",
            "language": "hcl",
            "source_dir": str(tmp_path),
            "codebase_type": "iac_terraform",
        }))
        assert resp.is_ok
        assert resp.payload["codebase_type"] == "iac_terraform"
        assert "terraform_azurerm_provider" in resp.payload["target_services"]
