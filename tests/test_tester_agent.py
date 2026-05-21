"""
tests/test_tester_agent.py — unit tests for Tester + sandboxed test_runner.

Covers:
  - make_run_tests: runs pytest in subprocess on a real test fixture and
    parses pass/fail summary.
  - sandbox: rejects test_dir outside the bound root; missing test_dir errors out.
  - env scrubbing: AZURE_OPENAI_KEY does not propagate into the subprocess.
  - parse_test_output: verdict extraction (PASS/FAIL/PARTIAL/UNKNOWN), structured
    failures from JSON blocks under the heading.
  - A2A handler validation + happy path with a stub agent.
  - Optional output_dir disk sink writes test-results.md and eval-failures.json.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from a2a.envelope import A2ARequest, A2AStatus
from agents._lib.test_runner import make_run_tests
from agents.tester_agent import (
    AGENT_TYPE,
    REPORT_SCHEMA,
    REQUEST_SCHEMA,
    TesterHandler,
    parse_test_output,
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
        sender="Coder-test", recipient=f"{AGENT_TYPE}-test",
        run_id="run-1", module_id="mod-1",
        intent="evaluate_module", payload_schema=schema, payload=payload,
    )


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _make_passing_test_module(root: Path) -> Path:
    """Create a tiny Python module + passing tests under root/.
    Returns the tests/ directory path."""
    module_dir = root / "demo_mod"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__init__.py").write_text("")
    (module_dir / "function_app.py").write_text(
        "def add(x, y):\n    return x + y\n", encoding="utf-8"
    )
    tests_dir = module_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_add.py").write_text(
        "from demo_mod.function_app import add\n"
        "def test_add(): assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return tests_dir


def _make_failing_test_module(root: Path) -> Path:
    module_dir = root / "demo_mod"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__init__.py").write_text("")
    (module_dir / "function_app.py").write_text(
        "def add(x, y):\n    return x - y\n", encoding="utf-8"  # bug
    )
    tests_dir = module_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_add.py").write_text(
        "from demo_mod.function_app import add\n"
        "def test_add(): assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return tests_dir


# ── run_tests sandbox + execution ────────────────────────────────────────────

class TestRunTestsSandbox:
    def test_rejects_path_outside_sandbox(self, tmp_path: Path):
        rt = make_run_tests(tmp_path)
        outside = tmp_path.parent / "elsewhere"
        outside.mkdir(exist_ok=True)
        out = rt.func(str(outside))
        assert out.startswith("ERROR: test_dir outside sandbox")

    def test_rejects_missing_test_dir(self, tmp_path: Path):
        rt = make_run_tests(tmp_path)
        out = rt.func(str(tmp_path / "ghost"))
        assert "does not exist" in out

    def test_function_tool_name_is_run_tests(self, tmp_path: Path):
        rt = make_run_tests(tmp_path)
        assert rt.name == "run_tests"


@pytest.mark.skipif(
    subprocess.run([os.sys.executable, "-m", "pytest", "--version"],
                   capture_output=True).returncode != 0,
    reason="pytest must be installed in the runtime to exercise the subprocess path",
)
class TestRunTestsExecution:
    def test_passing_suite_returns_status_pass(self, tmp_path: Path):
        tests_dir = _make_passing_test_module(tmp_path)
        rt = make_run_tests(tmp_path)
        out = rt.func(str(tests_dir))
        assert "Status: PASS" in out
        assert "Passed: 1" in out
        assert "Exit code: 0" in out

    def test_failing_suite_returns_status_fail(self, tmp_path: Path):
        tests_dir = _make_failing_test_module(tmp_path)
        rt = make_run_tests(tmp_path)
        out = rt.func(str(tests_dir))
        assert "Status: FAIL" in out
        assert "Failed: 1" in out
        assert "Exit code:" in out

    def test_secret_env_vars_do_not_leak_into_subprocess(self, tmp_path: Path, monkeypatch):
        # Set a sensitive env var in the parent process. The subprocess
        # must NOT see it (env scrubbing is the whole point of the sandbox).
        monkeypatch.setenv("AZURE_OPENAI_KEY", "leaky-secret-do-not-leak")

        # Test module that asserts AZURE_OPENAI_KEY is unset.
        module_dir = tmp_path / "leak_check"
        module_dir.mkdir()
        (module_dir / "__init__.py").write_text("")
        tests_dir = module_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_env.py").write_text(
            "import os\n"
            "def test_no_azure_key():\n"
            "    assert 'AZURE_OPENAI_KEY' not in os.environ\n",
            encoding="utf-8",
        )

        rt = make_run_tests(tmp_path)
        out = rt.func(str(tests_dir))
        # If env scrubbing failed, the assert raises and Status would be FAIL.
        assert "Status: PASS" in out, f"env leaked into subprocess; output:\n{out}"


# ── parse_test_output ────────────────────────────────────────────────────────

class TestParseTestOutput:
    def test_pass_verdict(self):
        md = "## Layer 1: Unit Tests\n- Total: 5\n## Overall Verdict: PASS\n"
        verdict, failures = parse_test_output(md)
        assert verdict == "PASS"
        assert failures == []

    def test_fail_with_structured_failures(self):
        md = (
            "## Overall Verdict: FAIL\n"
            "## Structured Failures\n"
            '{"failure_id": "F001", "layer": "unit", "error_category": "assertion_error", "description": "x"}\n'
            '{"failure_id": "F002", "layer": "unit", "error_category": "import_error", "description": "y"}\n'
        )
        verdict, failures = parse_test_output(md)
        assert verdict == "FAIL"
        assert len(failures) == 2
        assert failures[0]["failure_id"] == "F001"
        assert failures[1]["error_category"] == "import_error"

    def test_failures_inside_json_fence(self):
        md = (
            "## Overall Verdict: FAIL\n"
            "## Structured Failures\n"
            "```json\n"
            '{"failure_id": "F003", "layer": "contract", "error_category": "schema_mismatch", "description": "z"}\n'
            "```\n"
        )
        verdict, failures = parse_test_output(md)
        assert verdict == "FAIL"
        assert len(failures) == 1
        assert failures[0]["failure_id"] == "F003"

    def test_partial_verdict(self):
        md = "## Overall Verdict: PARTIAL\n"
        v, _ = parse_test_output(md)
        assert v == "PARTIAL"

    def test_unknown_when_unparseable(self):
        v, f = parse_test_output("nothing useful")
        assert v == "UNKNOWN"
        assert f == []

    def test_tail_scan_picks_up_fail_when_no_label(self):
        md = "long doc with no labelled verdict\n... eventually FAIL\n"
        v, _ = parse_test_output(md)
        assert v == "FAIL"

    def test_unparseable_failure_lines_skipped_silently(self):
        md = (
            "## Overall Verdict: FAIL\n"
            "## Structured Failures\n"
            '{"failure_id": "F001"}\n'
            "not-json line\n"
            '{"failure_id": "F002"}\n'
        )
        _, failures = parse_test_output(md)
        ids = [f["failure_id"] for f in failures]
        assert ids == ["F001", "F002"]


# ── A2A handler ──────────────────────────────────────────────────────────────

class TestHandlerValidation:
    @pytest.mark.asyncio
    async def test_schema_mismatch_returns_error(self):
        h = TesterHandler(agent=_FakeAgent(""))
        resp = await h.handle(_request({}, schema="WrongSchema/v1"))
        assert not resp.is_ok
        assert resp.payload["code"] == "schema_mismatch"

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_error(self):
        h = TesterHandler(agent=_FakeAgent(""))
        resp = await h.handle(_request({"module": "m"}))
        assert not resp.is_ok
        assert resp.payload["code"] == "invalid_payload"


class TestHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_returns_typed_test_report(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "function_app.py", "def main(): return 1\n")
        agent = _FakeAgent(reply_text=(
            "# Test Results: orders\n\n"
            "## Layer 1: Unit Tests\n- Total: 5 | Passed: 5 | Failed: 0\n\n"
            "## Overall Verdict: PASS\n"
        ))
        handler = TesterHandler(agent=agent, nhi_id="local-tester-nhi")
        resp = await handler.handle(_request({
            "module": "orders", "language": "python", "attempt": 1,
            "migrated_source_dir": str(src),
            "test_dir": str(src / "tests"),
        }))

        assert resp.is_ok
        assert resp.payload_schema == REPORT_SCHEMA
        body = resp.payload
        assert body["module"] == "orders"
        assert body["verdict"] == "PASS"
        assert body["failures"] == []

        opts = agent.captured_options[0]
        assert opts["extra_headers"]["x-galaxy-run-id"] == "run-1"

    @pytest.mark.asyncio
    async def test_failure_report_extracted_into_typed_field(self, tmp_path: Path):
        src = tmp_path / "src"
        _write(src / "function_app.py", "def main(): pass\n")
        agent = _FakeAgent(reply_text=(
            "## Overall Verdict: FAIL\n"
            "## Structured Failures\n"
            '{"failure_id": "F001", "layer": "unit", "error_category": "assertion_error", "description": "..."}\n'
        ))
        handler = TesterHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python", "attempt": 1,
            "migrated_source_dir": str(src),
            "test_dir": str(src / "tests"),
        }))
        assert resp.payload["verdict"] == "FAIL"
        assert resp.payload["failures"][0]["failure_id"] == "F001"

    @pytest.mark.asyncio
    async def test_output_dir_writes_test_results_and_eval_failures(self, tmp_path: Path):
        src = tmp_path / "src"
        out_root = tmp_path / "out"
        _write(src / "function_app.py", "x\n")
        agent = _FakeAgent(reply_text=(
            "## Overall Verdict: FAIL\n"
            "## Structured Failures\n"
            '{"failure_id": "F001", "layer": "unit", "error_category": "assertion_error", "description": "..."}\n'
        ))
        handler = TesterHandler(agent=agent)
        resp = await handler.handle(_request({
            "module": "m", "language": "python", "attempt": 1,
            "migrated_source_dir": str(src),
            "test_dir": str(src / "tests"),
            "output_dir": str(out_root),
        }))
        assert resp.is_ok
        results_md = out_root / "m" / "test-results.md"
        eval_failures = out_root / "m" / "eval-failures.json"
        assert results_md.is_file()
        assert eval_failures.is_file()
        parsed = json.loads(eval_failures.read_text())
        assert parsed["overall_verdict"] == "FAIL"
        assert parsed["failures"][0]["failure_id"] == "F001"
