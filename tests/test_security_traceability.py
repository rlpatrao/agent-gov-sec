"""
Security + traceability tests — MAF port.

Replaces the pre-MAF version (which imported the deleted foundry_client).
Targets:
  - TokenProvider: Key Vault fallback, invalidate semantics
  - NHIRegistry: registration + validation
  - governance.middleware: build_governance_stack wiring, compat shim
  - agents.scanner_agent.traverse_repo: deterministic filesystem scan
"""

from pathlib import Path

import pytest

from nhi_identity import AgentIdentity, NHIRegistry
from token_provider import TokenProvider


# ── TokenProvider ─────────────────────────────────────────────────────────────

class TestTokenProvider:
    def test_env_var_fallback_when_no_vault(self, monkeypatch):
        monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
        monkeypatch.setenv("TEST_FALLBACK_KEY", "env-key-value")
        tp = TokenProvider(vault_url=None, env_var_fallback="TEST_FALLBACK_KEY")
        assert tp.get_api_key() == "env-key-value"

    def test_env_var_missing_raises(self, monkeypatch):
        monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
        monkeypatch.delenv("NO_SUCH_KEY_VAR_XYZ", raising=False)
        tp = TokenProvider(vault_url=None, env_var_fallback="NO_SUCH_KEY_VAR_XYZ")
        with pytest.raises(EnvironmentError):
            tp.get_api_key()

    def test_invalidate_clears_cache(self, monkeypatch):
        monkeypatch.setenv("TEST_CACHE_KEY", "v1")
        tp = TokenProvider(vault_url=None, env_var_fallback="TEST_CACHE_KEY")
        assert tp.get_api_key() == "v1"
        monkeypatch.setenv("TEST_CACHE_KEY", "v2")
        assert tp.get_api_key() == "v1"  # still cached
        tp.invalidate()
        assert tp.get_api_key() == "v2"  # refreshed after invalidate


# ── NHIRegistry ───────────────────────────────────────────────────────────────

class TestNHIRegistry:
    def test_returns_identity_for_known_agent(self):
        identity = NHIRegistry.get("Scanner")
        assert identity.agent_type == "Scanner"
        assert identity.client_id

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError):
            NHIRegistry.get("NoSuchAgent")

    def test_agent_identity_str(self):
        i = AgentIdentity(agent_type="Scanner", client_id="abc-123")
        assert "Scanner" in str(i) and "abc-123" in str(i)


# ── governance.middleware ─────────────────────────────────────────────────────

class TestGovernanceStack:
    @pytest.mark.asyncio
    async def test_build_stack_loads_policies_and_returns_middleware(self):
        from governance.middleware import build_governance_stack
        middleware, pg, audit = await build_governance_stack(
            agent_id="Scanner-test-001",
            run_id="run-test-001",
        )
        try:
            names = {type(m).__name__ for m in middleware}
            assert "GovernancePolicyMiddleware" in names
            assert "AuditTrailMiddleware" in names
        finally:
            await pg.close()

    @pytest.mark.asyncio
    async def test_compat_shim_accepts_legacy_kwargs(self):
        """agent_os_kernel 3.2.2's maf_adapter calls audit_log.log() with
        legacy kwargs; _CompatAuditLogger must accept them and attach entry_id."""
        from agent_os.audit_logger import InMemoryBackend
        from governance.middleware import _CompatAuditLogger
        audit = _CompatAuditLogger()
        audit.add_backend(InMemoryBackend())
        entry = audit.log(
            event_type="policy_evaluation",
            agent_did="Scanner-test",
            action="allow",
            data={"matched_rule": "some-rule"},
            outcome="success",
            policy_decision="allow",
        )
        assert entry.event_type == "policy_evaluation"
        assert entry.action == "allow"
        assert entry.decision == "success"
        assert entry.metadata["matched_rule"] == "some-rule"
        assert hasattr(entry, "entry_id")


# ── scanner_agent.traverse_repo ───────────────────────────────────────────────

class TestTraverseRepo:
    def test_excludes_venv_and_classifies_python(self, tmp_path: Path):
        from agents.scanner_agent import traverse_repo
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text('if __name__ == "__main__":\n    pass\n')
        (tmp_path / "src" / "util.py").write_text("def helper(): pass\n")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "lib.py").write_text("# excluded")

        result = traverse_repo(str(tmp_path))
        assert result["detected_language"] == "python"
        assert "src/main.py" in result["files"]
        assert "src/util.py" in result["files"]
        assert not any(".venv" in f for f in result["files"])
        assert "src/main.py" in result["entry_points"]

    def test_not_a_directory_raises(self):
        from agents.scanner_agent import traverse_repo
        with pytest.raises(ValueError):
            traverse_repo("/no/such/path/xyz-xyz-xyz")
