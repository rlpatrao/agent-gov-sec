"""
Tests for the code-quality subgroup guard wrappers (Tier B).

Each guard exercises the REAL agent_os symbol (no mocking upstream) and asserts
both a PASS (allowed True) and an INTERCEPT (allowed False + expected code) case
drawn from the discovery demo_scenario. These are static checks — no user code
is executed — so there is no sleep/wall-clock risk.
"""

from __future__ import annotations

from governance.extensions.decision import GuardDecision
from governance.extensions.diff_policy_guard import DiffPolicyGuard
from governance.extensions.secure_codegen_guard import SecureCodegenGuard
from governance.extensions.secure_exec import SecureExecGuard


# --------------------------------------------------------------------------- #
# secure_codegen — CodeSecurityValidator                                      #
# --------------------------------------------------------------------------- #
def test_secure_codegen_pass():
    guard = SecureCodegenGuard()
    decision = guard.check_code("write_file", {"code": "def add(a, b):\n    return a + b"})
    assert isinstance(decision, GuardDecision)
    assert decision.allowed is True


def test_secure_codegen_intercept_shell_and_secret():
    guard = SecureCodegenGuard()
    bad = (
        "import subprocess\n"
        "subprocess.run(cmd, shell=True)\n"
        'api_key = "AKIA1234567890ABCDEF"'
    )
    decision = guard.check_code("apply_patch", {"code": bad})
    assert decision.allowed is False
    assert decision.code == "insecure_codegen"
    # shell-injection and hardcoded-secret should both surface in the rules.
    rules = decision.metadata.get("rules", [])
    assert "shell-injection" in rules
    assert "hardcoded-secret" in rules
    # sanitized_code rides on output for an audit-posture substitution.
    assert decision.output is not None
    assert "# REMOVED (security):" in decision.output


# --------------------------------------------------------------------------- #
# sandbox — ExecutionSandbox.validate_code                                    #
# --------------------------------------------------------------------------- #
def test_secure_exec_pass():
    guard = SecureExecGuard()
    decision = guard.check_exec("python_exec", {"code": "x = 1 + 2\nprint(x)"})
    assert decision.allowed is True


def test_secure_exec_intercept_os_system():
    guard = SecureExecGuard()
    decision = guard.check_exec(
        "run_code", {"code": 'import os\nos.system("rm -rf /")'}
    )
    assert decision.allowed is False
    assert decision.code == "unsafe_exec"
    # blocked import of os plus the blocked os.system call.
    types = decision.metadata.get("violation_types", [])
    assert "blocked_import" in types
    assert "blocked_module_call" in types


# --------------------------------------------------------------------------- #
# diff_policy — DiffPolicy.evaluate                                           #
# --------------------------------------------------------------------------- #
def _diff_guard() -> DiffPolicyGuard:
    return DiffPolicyGuard(
        max_files=10, max_lines=400, blocked_paths=["*.env", "secrets/**"]
    )


def test_diff_policy_pass():
    guard = _diff_guard()
    decision = guard.check_diff(
        "create_pr",
        {"files": [{"path": "src/app.py", "additions": 20, "deletions": 5}]},
    )
    assert decision.allowed is True


def test_diff_policy_intercept_blocked_paths():
    guard = _diff_guard()
    decision = guard.check_diff(
        "create_pr",
        {
            "files": [
                {"path": ".env", "additions": 3, "deletions": 0},
                {"path": "secrets/key.pem", "additions": 50, "deletions": 0},
            ]
        },
    )
    assert decision.allowed is False
    assert decision.code == "diff_policy_denied"
    violations = decision.metadata.get("violations", [])
    assert any(".env" in v for v in violations)
    assert any("secrets/key.pem" in v for v in violations)
