"""
agents/_lib/test_runner.py — sandboxed pytest runner for Tester.

Closure-based factory: each Tester build gets its own bound `run_tests`
whose `allowed_test_root` is fixed at construction. Caller passes the
migrated module's test directory; `run_tests` rejects any test_dir that
escapes the sandbox.

Differences from agentrepo's tools/test_runner.py:
  - Sandboxed: test_dir must be within `allowed_test_root` or call refuses.
  - cwd lockdown: subprocess runs with cwd=test_dir, not the caller's cwd.
  - Sanitized env: only PATH, HOME, LANG, LC_*, and PYTHONPATH (computed
    from the module dir) are passed through. Caller env vars including
    secrets like AZURE_OPENAI_KEY do NOT reach the subprocess.
  - Configurable timeout (default 120s); always non-None to prevent hangs.
  - jest path dropped — Phase 2B is Python only; can extend later.
  - measure_coverage dropped — adds a pytest-cov dependency we don't need
    in the reference port. Coder tests already enforce coverage; the
    Tester verdict is pass/fail on the unit suite.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from agent_framework import tool

logger = logging.getLogger(__name__)

# Caller env vars that survive into the pytest subprocess. Secrets and
# Azure-specific config are explicitly NOT inherited — tests must mock.
_PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _build_subprocess_env(test_path: Path) -> dict[str, str]:
    """Curated env for the pytest subprocess.

    Mirrors agentrepo's `_prepare_python_env` for PYTHONPATH (parent of
    test_dir is on the path so `from <module>.function_app import ...`
    works) but starts from a clean env instead of os.environ.copy() so
    AZURE_OPENAI_*, NHI_*, APIM_* don't leak into the test process.
    """
    env: dict[str, str] = {
        k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ
    }
    module_dir = test_path.parent if test_path.name == "tests" else test_path
    parent = module_dir.parent
    env["PYTHONPATH"] = f"{parent}{os.pathsep}{module_dir}"
    # Mark this so test code can detect it's running under our runner if needed.
    env["GALAXY_TEST_RUNNER"] = "1"
    return env


def _ensure_module_init(module_dir: Path) -> None:
    """Add an empty __init__.py if missing — pytest needs the module dir
    to be a package for `from <module> import ...` patterns the Coder writes.
    """
    if not module_dir.is_dir():
        return
    init = module_dir / "__init__.py"
    if not init.exists():
        try:
            init.touch()
        except OSError:
            pass


def make_run_tests(
    allowed_test_root: str | Path,
    *,
    timeout_seconds: int = 120,
) -> Any:
    """Build a sandboxed run_tests tool bound to `allowed_test_root`.

    The returned MAF FunctionTool refuses any test_dir outside the root.
    """
    root = Path(allowed_test_root).resolve()

    @tool(approval_mode="never_require")
    def run_tests(test_dir: str) -> str:
        """Run pytest against test_dir. Returns a pass/fail summary.

        The test_dir must be inside the agent's sandbox (the migrated
        module's tests directory). Subprocess runs with cwd=test_dir,
        a curated env (no Azure/APIM secrets), and a hard timeout.
        """
        target = Path(test_dir)
        if not _is_within(target, root):
            return (f"ERROR: test_dir outside sandbox: {test_dir}. "
                    f"Allowed root: {root}")
        if not target.is_dir():
            return f"ERROR: test_dir does not exist: {test_dir}"

        module_dir = target.parent if target.name == "tests" else target
        _ensure_module_init(module_dir)

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(target),
                 "-v", "--tb=short", "--no-header"],
                capture_output=True, text=True,
                timeout=timeout_seconds,
                env=_build_subprocess_env(target),
                cwd=str(target),
            )
        except FileNotFoundError:
            return "ERROR: pytest not installed in agent runtime"
        except subprocess.TimeoutExpired:
            return f"ERROR: tests timed out after {timeout_seconds}s"

        output = (result.stdout or "") + (result.stderr or "")
        # Last 2000 chars only — the LLM doesn't need a 100K-line traceback.
        tail = output[-2000:]

        passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", output)) else 0
        failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", output)) else 0
        errors = int(m.group(1)) if (m := re.search(r"(\d+) error", output)) else 0
        total = passed + failed + errors

        status = "PASS" if result.returncode == 0 else "FAIL"
        return (
            f"Status: {status}\n"
            f"Total: {total} | Passed: {passed} | Failed: {failed} | Errors: {errors}\n"
            f"Exit code: {result.returncode}\n\n"
            f"Output (tail):\n{tail}"
        )

    return run_tests
