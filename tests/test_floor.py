"""
tests/test_floor.py — the non-overridable governance floor (mechanism 2).

The floor may only make a per-agent config stricter, never looser, and every
field it clamps must be reported as a violation.
"""

from __future__ import annotations

from governance.floor import GovernanceFloor, apply_floor
from payload_agents.config import GovernanceConfig, load_agent_config


class TestFloorClampsWeakening:
    def test_disabled_guard_is_forced_on_and_reported(self):
        cfg = GovernanceConfig(enable_prompt_injection_guard=False)
        clamped, violations = apply_floor(cfg)
        assert clamped.enable_prompt_injection_guard is True
        assert any(v.field == "enable_prompt_injection_guard" for v in violations)

    def test_all_four_required_guards_are_forced_on(self):
        # Pre-seed the mandatory blocked patterns and the data gates so only the
        # four core guard toggles clamp.
        cfg = GovernanceConfig(
            enable_prompt_injection_guard=False,
            enable_credential_redactor=False,
            enable_context_budget=False,
            enable_rogue_detection=False,
            blocked_patterns=list(GovernanceFloor().mandatory_blocked_patterns),
            enable_data_fgac=True, enable_data_drift=True, enable_reasoning_guard=True,
        )
        clamped, violations = apply_floor(cfg)
        assert clamped.enable_prompt_injection_guard is True
        assert clamped.enable_credential_redactor is True
        assert clamped.enable_context_budget is True
        assert clamped.enable_rogue_detection is True
        assert len(violations) == 4

    def test_loose_block_threshold_is_clamped_down(self):
        cfg = GovernanceConfig(prompt_injection_block_threshold="critical")
        clamped, violations = apply_floor(cfg)
        assert clamped.prompt_injection_block_threshold == "high"
        assert any(v.field == "prompt_injection_block_threshold" for v in violations)

    def test_oversized_context_budget_is_capped(self):
        cfg = GovernanceConfig(context_budget_tokens=200_000)
        clamped, violations = apply_floor(cfg)
        assert clamped.context_budget_tokens == GovernanceFloor().max_context_budget_tokens
        assert any(v.field == "context_budget_tokens" for v in violations)

    def test_mandatory_blocked_patterns_are_unioned_in(self):
        cfg = GovernanceConfig(blocked_patterns=["custom"])
        clamped, violations = apply_floor(cfg)
        assert "custom" in clamped.blocked_patterns
        assert "DROP TABLE" in clamped.blocked_patterns
        assert any(v.field == "blocked_patterns" for v in violations)


class TestFloorAllowsTightening:
    def test_stricter_credential_mode_is_kept(self):
        # deny is stricter than the redact floor — must not be loosened.
        cfg = GovernanceConfig(credential_mode="deny")
        clamped, violations = apply_floor(cfg)
        assert clamped.credential_mode == "deny"
        assert not any(v.field == "credential_mode" for v in violations)

    def test_stricter_threshold_is_kept(self):
        cfg = GovernanceConfig(prompt_injection_block_threshold="medium")
        clamped, _ = apply_floor(cfg)
        assert clamped.prompt_injection_block_threshold == "medium"

    def test_config_already_at_floor_yields_no_violations(self):
        # A config that already meets every floor field — the mandatory blocked
        # patterns AND the required data-layer gates — passes through untouched.
        cfg = GovernanceConfig(
            blocked_patterns=list(GovernanceFloor().mandatory_blocked_patterns),
            enable_data_fgac=True, enable_data_drift=True, enable_reasoning_guard=True,
        )
        clamped, violations = apply_floor(cfg)
        assert violations == []
        assert clamped is cfg


class TestFloorFailsClosedOnDataGates:
    """The data-layer/reasoning gates default off in the schema; the floor must
    force them on so omitting them in a YAML cannot silently disable them."""

    def test_omitted_data_gates_are_forced_on(self):
        cfg = GovernanceConfig(blocked_patterns=list(GovernanceFloor().mandatory_blocked_patterns))
        # schema defaults: fgac/drift/reasoning all False
        assert cfg.enable_data_fgac is False
        clamped, violations = apply_floor(cfg)
        assert clamped.enable_data_fgac is True
        assert clamped.enable_data_drift is True
        assert clamped.enable_reasoning_guard is True
        fields = {v.field for v in violations}
        assert {"enable_data_fgac", "enable_data_drift", "enable_reasoning_guard"} <= fields


class TestShippedConfigsMeetFloor:
    """The three demo personas must satisfy the floor with zero clamping, so the
    baseline matrix (37/37) does not regress when the floor is wired in."""

    def test_finops_unclamped(self):
        _assert_no_clamp("finops")

    def test_auditor_unclamped(self):
        _assert_no_clamp("auditor")

    def test_rogue_unclamped(self):
        _assert_no_clamp("rogue")


def _assert_no_clamp(agent_name: str) -> None:
    cfg = load_agent_config(agent_name)
    _, violations = apply_floor(cfg.governance)
    assert violations == [], f"{agent_name} config triggers floor clamps: {[str(v) for v in violations]}"
