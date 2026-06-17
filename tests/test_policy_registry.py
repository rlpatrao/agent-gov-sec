"""
tests/test_policy_registry.py — the NHI-keyed control authority.

Resolution applies the floor, export round-trips, and lookups are fail-closed.
"""

from __future__ import annotations

from governance.policy_registry import (
    authorize_recipient,
    export_registry,
    load_registry,
    policy_for,
    resolve_policy,
)


class TestResolve:
    def test_finops_posture_resolved(self):
        p = resolve_policy("finops")
        assert p.agent_type == "FinOps"
        assert p.allowed_tools == ("query_billing", "summarize_costs")
        assert p.allowed_recipients == ("Auditor",)
        assert p.data_fgac is True
        assert p.model_boundary["credential_mode"] == "redact"

    def test_resolution_reflects_floor(self):
        # The floor forces the data gates on; a resolved policy always has them.
        for name in ("finops", "auditor", "rogue"):
            p = resolve_policy(name)
            assert p.data_fgac and p.data_drift and p.reasoning_guard


class TestExportAndLookup:
    def test_export_contains_known_agents(self):
        reg = export_registry()
        assert set(reg["agents"]) == {"FinOps", "Auditor", "Rogue"}
        assert reg["default"] == "deny"

    def test_round_trip_through_json(self):
        import json
        reg = load_registry(json.dumps(export_registry()))
        assert policy_for(reg, "FinOps")["allowed_tools"] == ["query_billing", "summarize_costs"]

    def test_unknown_identity_is_fail_closed(self):
        reg = export_registry()
        assert policy_for(reg, "Ghost") is None
        assert policy_for(reg, None) is None
        assert policy_for({}, "FinOps") is None


class TestAuthorizeRecipient:
    def test_in_process_resolution(self):
        ok, _ = authorize_recipient("FinOps", "Auditor-123")
        assert ok
        ok2, reason = authorize_recipient("FinOps", "Rogue-123")
        assert not ok2 and "may not dispatch" in reason

    def test_unknown_sender_denied(self):
        ok, reason = authorize_recipient("Ghost", "Auditor")
        assert not ok and "no governance policy" in reason

    def test_registry_backed_resolution(self):
        reg = export_registry()
        ok, _ = authorize_recipient("FinOps", "Auditor", reg)
        assert ok
