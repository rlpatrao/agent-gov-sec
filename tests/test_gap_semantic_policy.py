"""Tests for the semantic-policy per-call guard wrapper.

Exercises the real ``agent_os.semantic_policy.SemanticPolicyEngine`` through
``SemanticPolicyGuard`` (no upstream mocking). Asserts a PASS case (a benign
SELECT) and INTERCEPT cases (``DROP TABLE`` and ``rm -rf``), per the discovery
demo scenario.
"""

from agent_os.semantic_policy import IntentCategory

from governance.extensions.semantic_policy_guard import SemanticPolicyGuard


def _guard() -> SemanticPolicyGuard:
    return SemanticPolicyGuard(
        deny=[IntentCategory.DESTRUCTIVE_DATA, IntentCategory.PRIVILEGE_ESCALATION],
        confidence_threshold=0.5,
    )


def test_pass_benign_select() -> None:
    guard = _guard()
    decision = guard.check_tool(
        "database_query", {"query": "SELECT id FROM users WHERE active = 1"}
    )
    assert decision.allowed is True
    assert decision.metadata["category"] == IntentCategory.DATA_READ.value


def test_intercept_drop_table() -> None:
    guard = _guard()
    decision = guard.check_tool("database_query", {"query": "DROP TABLE users"})
    assert decision.allowed is False
    assert decision.code == "semantic_policy_denied"
    assert decision.metadata["category"] == IntentCategory.DESTRUCTIVE_DATA.value


def test_intercept_rm_rf() -> None:
    guard = _guard()
    decision = guard.check_tool("shell", {"cmd": "rm -rf /"})
    assert decision.allowed is False
    assert decision.code == "semantic_policy_denied"
    # rm -rf is recursive force delete -> SYSTEM_MODIFICATION, caught via the
    # broader is_dangerous path even though it is outside the deny set.
    assert decision.metadata["category"] == IntentCategory.SYSTEM_MODIFICATION.value


def test_sample_signals_warning_suppressed_and_recorded() -> None:
    # Constructing with no config uses sample signals; the guard suppresses the
    # UserWarning locally but records that sample signals are in effect.
    guard = _guard()
    assert guard.using_sample_signals is True
