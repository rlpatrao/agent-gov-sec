"""
tests/test_azure_adapter.py — the Azure adapter (agnostic; no Azure SDK required).

Exercises cloud_adapters/azure against the core interfaces with the Azure SDK
forced absent (monkeypatched out), mirroring tests/test_aws_adapter.py. Verifies:
factory resolution, secret env-var fallback, identity graceful degradation, the
egress allow-list, stdout-mode audit (Postgres backend), the store-side FGAC
pushdown (Azure SQL / Synapse), the out-of-process Function chokepoints
(llm / data / a2a) failing closed, and the Container-Apps-Jobs orchestrator
degrading without the management SDK. Live Azure (real AOAI/Key Vault/Postgres)
is not exercised here.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from core.provider_factory import get_provider

_AZURE_EGRESS = Path(__file__).parent.parent / "cloud_adapters" / "azure" / "egress.yaml"
_CATALOG = Path(__file__).parent.parent / "galaxy_gov" / "shared" / "enforcement" / "configs" / "data-classification.example.yaml"


# ── Factory + protocol conformance ────────────────────────────────────────────

def test_factory_resolves_azure():
    p = get_provider("azure")
    assert p.name == "azure"
    assert p.identity_provider() is not None
    assert p.trace_exporter_factory() is not None
    assert p.llm_gateway() is not None
    from core.interfaces import AgentRuntimeAdapter
    rt = p.runtime_adapter()
    assert rt is not None and isinstance(rt, AgentRuntimeAdapter)
    egress = p.egress_config_path()
    assert egress is not None and egress.name == "egress.yaml"


def test_azure_impls_satisfy_protocols():
    from core.interfaces import IdentityProvider, LLMGateway, SecretProvider, TraceExporterFactory
    from cloud_adapters.azure.gateway import AzureLLMGateway
    from cloud_adapters.azure.identity import AzureIdentityProvider
    from cloud_adapters.azure.secrets import TokenProvider
    from cloud_adapters.azure.tracing import AzureTraceExporterFactory

    assert isinstance(AzureIdentityProvider(), IdentityProvider)
    assert isinstance(TokenProvider(env_var_fallback="X"), SecretProvider)
    assert isinstance(AzureTraceExporterFactory(), TraceExporterFactory)
    assert isinstance(AzureLLMGateway(), LLMGateway)


# ── Secrets: env-var fallback (local mode, no Key Vault) ──────────────────────

def test_azure_secret_env_fallback(monkeypatch):
    monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_KEY", "aoai-secret-xyz")
    from cloud_adapters.azure.secrets import TokenProvider
    sp = TokenProvider(env_var_fallback="AZURE_OPENAI_KEY")
    assert sp.get_api_key() == "aoai-secret-xyz"


def test_azure_secret_missing_raises(monkeypatch):
    monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("ABSENT_AOAI_KEY", raising=False)
    from cloud_adapters.azure.secrets import TokenProvider
    sp = TokenProvider(env_var_fallback="ABSENT_AOAI_KEY")
    with pytest.raises(EnvironmentError, match="ABSENT_AOAI_KEY"):
        sp.get_api_key()


# ── Identity: graceful degradation without the SDK ────────────────────────────

def test_azure_identity_degrades_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "azure.identity", None)  # force ImportError
    from cloud_adapters.azure.identity import AzureIdentityProvider
    prov = AzureIdentityProvider()
    assert prov.get_credential(client_id="00000000-0000-0000-0000-000000000000", agent_type="FinOps") is None
    assert prov.get_credential(client_id="", agent_type="FinOps") is None


def test_azure_resolve_client_id_env_then_none(monkeypatch):
    from cloud_adapters.azure.identity import AzureIdentityProvider
    prov = AzureIdentityProvider()
    # 1) env (IaC-provisioned Entra clientId) wins
    monkeypatch.setenv("NHI_CLIENT_ID_FINOPS", "11111111-2222-3333-4444-555555555555")
    assert prov.resolve_client_id(agent_type="FinOps") == "11111111-2222-3333-4444-555555555555"
    # 2) unprovisioned agent + live lookup off → fail closed (None)
    monkeypatch.delenv("NHI_CLIENT_ID_ROGUE", raising=False)
    monkeypatch.delenv("GALAXY_ENTRA_LOOKUP", raising=False)
    assert prov.resolve_client_id(agent_type="Rogue") is None


# ── Egress allow-list ─────────────────────────────────────────────────────────

def test_azure_egress_loads_from_path():
    from galaxy_gov.shared.enforcement.guards.egress import load_egress_policy
    policy = load_egress_policy(yaml_path=_AZURE_EGRESS)
    assert policy.check_url("https://example-apim.azure-api.net/openai/").allowed is True
    assert policy.check_url("https://example-openai.openai.azure.com/").allowed is True
    assert policy.check_url("https://evil.example.com/").allowed is False


def test_azure_egress_resolves_via_factory(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "azure")
    from galaxy_gov.shared.enforcement.guards.egress import load_egress_policy
    policy = load_egress_policy()
    assert policy.check_url("https://example-apim.azure-api.net/").allowed is True
    assert policy.check_url("https://evil.example.com/").allowed is False


# ── Audit: stdout (no-persistence) mode when the SDK/DSN is absent ────────────

def test_azure_audit_stdout_mode_without_dsn(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    from cloud_adapters.azure.audit import PostgresHashChainBackend
    from agent_os.audit_logger import AuditEntry

    backend = asyncio.run(PostgresHashChainBackend.create(run_id="run-1"))
    assert backend._pool is None  # stdout mode

    e1 = AuditEntry(event_type="prompt_injection_blocked", agent_id="FinOps-run-1",
                    decision="deny", reason="test", metadata={"module_id": "m", "run_id": "run-1"})
    e2 = AuditEntry(event_type="credential_redacted", agent_id="FinOps-run-1",
                    decision="audit", reason="test2", metadata={"module_id": "m", "run_id": "run-1"})
    backend.write(e1)
    backend.write(e2)
    assert backend._entry_count == 2
    # Hash chain advances and links: e2's prev_hash == e1's entry_hash.
    assert backend._buffer[1][2] == backend._buffer[0][1]
    backend.flush()  # no-op, must not raise
    asyncio.run(backend.flush_async())  # no pool → clears buffer, no raise
    assert backend._buffer == []
    assert asyncio.run(backend.verify_chain()) is True  # no pool → trivially true


# ── Gap 1 cloud-native FGAC pushdown (Azure SQL / Synapse) ────────────────────

def _finops_decision():
    from galaxy_gov.shared.enforcement.data_classification import DataClassificationCatalog
    from galaxy_gov.shared.enforcement.data_fgac import DataAccessMediator
    med = DataAccessMediator(catalog=DataClassificationCatalog.load(_CATALOG))
    return med.authorize(
        agent_type="FinOps", dataset="finops", table="billing",
        columns=["account_id", "cost_usd", "region", "customer_email", "tax_id"],
    )


def test_azure_fgac_scoped_query_projects_masks_and_filters():
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    sql = AzureSqlFgacEnforcer().scoped_query(_finops_decision(), database="dbo", table="billing")
    # allowed columns projected (identifiers are bracket-quoted for injection safety)
    assert "[account_id]" in sql and "[cost_usd]" in sql and "[region]" in sql
    assert "FROM [dbo].[billing]" in sql
    # masked columns redacted at the store (the raw value is never selected)
    assert "AS [customer_email]" in sql and "AS [tax_id]" in sql
    assert "'***REDACTED***'" in sql
    # row filter pushed down as WHERE ... IN (...)
    assert "WHERE [region] IN ('us-east-1', 'us-west-2')" in sql


def test_azure_fgac_rejects_injection_in_identifiers():
    from galaxy_gov.shared.enforcement.data_fgac import DataAccessDecision
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    enf = AzureSqlFgacEnforcer()
    bad_col = DataAccessDecision(agent_type="FinOps", dataset="finops", table="billing",
                                 allowed_columns=["cost_usd FROM x; DROP TABLE y --"])
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        enf.scoped_query(bad_col, database="dbo", table="billing")
    ok_cols = DataAccessDecision(agent_type="FinOps", dataset="finops", table="billing",
                                 allowed_columns=["cost_usd"])
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        enf.scoped_query(ok_cols, database="dbo; DROP TABLE x", table="billing")


def test_azure_fgac_scoped_query_denied_raises():
    from galaxy_gov.shared.enforcement.data_fgac import DataAccessDecision
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    denied = DataAccessDecision(agent_type="FinOps", dataset="hr", table="employees", denied=True, reason="out of scope")
    with pytest.raises(PermissionError, match="denied"):
        AzureSqlFgacEnforcer().scoped_query(denied, database="dbo", table="employees")


def test_azure_fgac_apply_is_defense_in_depth():
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    dec = _finops_decision()
    rows = [{"account_id": "a1", "cost_usd": 1, "region": "us-east-1", "customer_email": "x@y.com", "tax_id": "T-1"},
            {"account_id": "a2", "cost_usd": 2, "region": "eu-west-1", "customer_email": "z@y.com", "tax_id": "T-2"}]
    out = AzureSqlFgacEnforcer().apply(dec, rows)
    assert len(out) == 1 and out[0]["region"] == "us-east-1"
    assert out[0]["customer_email"] == "***REDACTED***" and out[0]["tax_id"] == "***REDACTED***"


def test_azure_fgac_register_rls_emits_ddl():
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    reg = AzureSqlFgacEnforcer().register_row_level_security(_finops_decision(), database="dbo", table="billing")
    assert reg["applied"] is False
    # column include-list GRANT + a row-filter security policy, masked cols excluded
    assert "GRANT SELECT ([account_id], [cost_usd], [region])" in reg["ddl"]
    assert "CREATE SECURITY POLICY" in reg["ddl"] and "FILTER PREDICATE" in reg["ddl"]
    assert "customer_email" not in reg["ddl"]  # masked column never granted


def test_azure_fgac_register_rls_apply_requires_pyodbc(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyodbc", None)
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    with pytest.raises(RuntimeError, match="pyodbc"):
        AzureSqlFgacEnforcer().register_row_level_security(_finops_decision(), database="dbo", table="billing", apply=True)


def test_azure_fgac_satisfies_enforcer_protocol():
    from galaxy_gov.shared.enforcement.data_fgac import DataAccessEnforcer
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    assert isinstance(AzureSqlFgacEnforcer(), DataAccessEnforcer)


# ── Method-1 out-of-process chokepoints (Azure Functions) fail closed ─────────

def test_azure_a2a_broker_validates_and_denies():
    from cloud_adapters.azure.infra.functions.a2a_broker import enforce_a2a
    assert enforce_a2a({}, {})[0] == 400                       # missing fields
    # unknown sender (empty registry) is denied, not allowed
    status, payload = enforce_a2a({"recipient": "Auditor"}, {"x-agent-type": "finops"})
    assert status == 403 and payload["error"] == "recipient_not_allowed"


def test_azure_data_proxy_validates_input():
    from cloud_adapters.azure.infra.functions.data_proxy import enforce_data
    assert enforce_data({}, {"x-agent-type": "finops"})[0] == 400  # missing dataset/table


def test_azure_llm_proxy_denies_without_policy():
    from cloud_adapters.azure.infra.functions.llm_proxy import enforce_llm
    status, payload = enforce_llm({"messages": [{"role": "user", "content": "hi"}]}, {"x-agent-type": "finops"})
    assert status == 403 and payload["error"] == "no_governance_policy"


# ── Orchestrator: degrades without the management SDK ─────────────────────────

def test_azure_orchestrator_requires_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "azure.mgmt.appcontainers", None)  # force ImportError
    from cloud_adapters.azure.orchestrator import submit_agent_job
    with pytest.raises(RuntimeError, match="azure-mgmt-appcontainers"):
        submit_agent_job(agent_type="finops", run_id="r1", module_id="m1",
                         subscription_id="sub", resource_group="rg")
