"""
tests/test_aws_adapter.py — the WS5 AWS adapter (agnostic; no boto3 required).

Exercises adapters/aws against the core interfaces with the AWS SDK forced
absent (monkeypatched out), mirroring the Azure/agnostic test tiers. Verifies:
factory resolution, secret env-var fallback, identity graceful degradation,
the gateway egress contract (API Gateway vs direct-Bedrock), egress allow-list
loading, and stdout-mode audit. Live AWS (real STS/Bedrock/DynamoDB) is not
exercised here — see docs/REFACTOR_AND_GAPS_PLAN.md WS5.9.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from core.provider_factory import get_provider

_AWS_EGRESS = Path(__file__).parent.parent / "adapters" / "aws" / "egress.yaml"


# ── Factory + protocol conformance ────────────────────────────────────────────

def test_factory_resolves_aws():
    p = get_provider("aws")
    assert p.name == "aws"
    # Real objects now (no NotImplementedError) without the AWS SDK installed.
    assert p.identity_provider() is not None
    assert p.trace_exporter_factory() is not None
    assert p.llm_gateway() is not None
    # AWS uses its own framework adapter (WS5.8), not MAF.
    assert p.runtime_adapter() is None
    egress = p.egress_config_path()
    assert egress is not None and egress.name == "egress.yaml"


def test_aws_impls_satisfy_protocols():
    from core.interfaces import IdentityProvider, LLMGateway, SecretProvider, TraceExporterFactory
    from adapters.aws.gateway import AwsLLMGateway
    from adapters.aws.identity import AwsIdentityProvider
    from adapters.aws.secrets import SecretsManagerProvider
    from adapters.aws.tracing import AwsTraceExporterFactory

    assert isinstance(AwsIdentityProvider(), IdentityProvider)
    assert isinstance(SecretsManagerProvider(env_var_fallback="X"), SecretProvider)
    assert isinstance(AwsTraceExporterFactory(), TraceExporterFactory)
    assert isinstance(AwsLLMGateway(), LLMGateway)


# ── Secrets: env-var fallback when the SDK is absent ──────────────────────────

def test_aws_secret_env_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # force ImportError on `import boto3`
    monkeypatch.setenv("AWS_LLM_API_KEY", "aws-secret-xyz")
    from adapters.aws.secrets import SecretsManagerProvider
    sp = SecretsManagerProvider(env_var_fallback="AWS_LLM_API_KEY")
    assert sp.get_api_key() == "aws-secret-xyz"


def test_aws_secret_missing_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.delenv("ABSENT_AWS_KEY", raising=False)
    from adapters.aws.secrets import SecretsManagerProvider
    sp = SecretsManagerProvider(env_var_fallback="ABSENT_AWS_KEY")
    with pytest.raises(EnvironmentError, match="ABSENT_AWS_KEY"):
        sp.get_api_key()


# ── Identity: graceful degradation without the SDK ────────────────────────────

def test_aws_identity_degrades_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    from adapters.aws.identity import AwsIdentityProvider
    prov = AwsIdentityProvider()
    assert prov.get_credential(client_id="arn:aws:iam::123:role/galaxy-analyzer", agent_type="Analyzer") is None
    assert prov.get_credential(client_id="", agent_type="Analyzer") is None


# ── Gateway: API Gateway vs direct-Bedrock egress contract ────────────────────

class _FakeSecret:
    def get_api_key(self) -> str:
        return "gw-key"

    def invalidate(self) -> None:
        pass


def test_aws_gateway_apigw_mode(monkeypatch):
    monkeypatch.setenv("AWS_BEDROCK_GATEWAY_ENDPOINT", "https://example-gw.execute-api.us-east-1.amazonaws.com")
    from adapters.aws.gateway import AwsLLMGateway
    res = AwsLLMGateway().resolve(agent_type="Analyzer", client_id="arn-1", secret_provider=_FakeSecret())
    assert res.mode == "apigw-bedrock"
    assert res.endpoint == "https://example-gw.execute-api.us-east-1.amazonaws.com"
    assert res.api_key == "gw-key"
    assert res.default_headers["x-api-key"] == "gw-key"
    assert res.default_headers["x-agent-type"] == "Analyzer"
    assert res.default_headers["x-nhi-id"] == "arn-1"


def test_aws_gateway_direct_bedrock_mode(monkeypatch):
    monkeypatch.delenv("AWS_BEDROCK_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.delenv("AWS_BEDROCK_ENDPOINT", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    from adapters.aws.gateway import AwsLLMGateway
    res = AwsLLMGateway().resolve(agent_type="Analyzer", client_id="arn-1")
    assert res.mode == "bedrock-direct"
    assert res.endpoint == "https://bedrock-runtime.us-east-1.amazonaws.com"
    assert res.api_key == ""  # SigV4/IAM — no static key changes hands
    assert "x-api-key" not in res.default_headers
    assert res.default_headers["x-agent-type"] == "Analyzer"
    assert res.default_headers["x-nhi-id"] == "arn-1"


# ── Egress allow-list ─────────────────────────────────────────────────────────

def test_aws_egress_loads_from_path():
    from governance.guards.egress import load_egress_policy
    policy = load_egress_policy(yaml_path=_AWS_EGRESS)
    assert policy.check_url("https://bedrock-runtime.us-east-1.amazonaws.com/model/invoke").allowed is True
    assert policy.check_url("https://secretsmanager.us-east-1.amazonaws.com/").allowed is True
    assert policy.check_url("https://evil.example.com/").allowed is False


def test_aws_egress_resolves_via_factory(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    from governance.guards.egress import load_egress_policy
    policy = load_egress_policy()
    assert policy.check_url("https://bedrock-runtime.us-east-1.amazonaws.com/").allowed is True
    assert policy.check_url("https://evil.example.com/").allowed is False


# ── Audit: stdout (no-persistence) mode when the SDK/table is absent ──────────

def test_aws_audit_stdout_mode_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    from adapters.aws.audit import DynamoDbHashChainBackend
    from agent_os.audit_logger import AuditEntry

    backend = asyncio.run(DynamoDbHashChainBackend.create(run_id="run-1"))
    assert backend._table is None  # stdout mode

    e1 = AuditEntry(event_type="prompt_injection_blocked", agent_id="Analyzer-run-1",
                    decision="deny", reason="test", metadata={"module_id": "m", "run_id": "run-1"})
    e2 = AuditEntry(event_type="credential_redacted", agent_id="Analyzer-run-1",
                    decision="audit", reason="test2", metadata={"module_id": "m", "run_id": "run-1"})
    backend.write(e1)
    backend.write(e2)
    assert backend._entry_count == 2
    # Hash chain advances and links: e2's prev_hash == e1's entry_hash.
    assert backend._buffer[1][2] == backend._buffer[0][1]
    backend.flush()  # no-op, must not raise
    asyncio.run(backend.flush_async())  # no table → clears buffer, no raise
    assert backend._buffer == []
    assert asyncio.run(backend.verify_chain()) is True  # no table → trivially true


# ── Gap 1 cloud-native FGAC pushdown (Lake Formation / Athena) ────────────────

_CATALOG = Path(__file__).parent.parent / "governance" / "extensions" / "configs" / "data-classification.example.yaml"


def _finops_decision():
    from governance.extensions.data_classification import DataClassificationCatalog
    from governance.extensions.data_fgac import DataAccessMediator
    med = DataAccessMediator(catalog=DataClassificationCatalog.load(_CATALOG))
    return med.authorize(
        agent_type="FinOps", dataset="finops", table="billing",
        columns=["account_id", "cost_usd", "region", "customer_email", "tax_id"],
    )


def test_aws_fgac_scoped_query_projects_masks_and_filters():
    from adapters.aws.data_fgac import AwsLakeFormationEnforcer
    sql = AwsLakeFormationEnforcer().scoped_query(_finops_decision(), database="finops", table="billing")
    # allowed columns projected
    assert "account_id" in sql and "cost_usd" in sql and "region" in sql
    assert "FROM finops.billing" in sql
    # masked columns redacted at the store (the raw value is never selected)
    assert "AS customer_email" in sql and "AS tax_id" in sql
    assert "'***REDACTED***'" in sql
    # row filter pushed down as WHERE ... IN (...)
    assert "WHERE region IN ('us-east-1', 'us-west-2')" in sql


def test_aws_fgac_scoped_query_denied_raises():
    from governance.extensions.data_fgac import DataAccessDecision
    from adapters.aws.data_fgac import AwsLakeFormationEnforcer
    denied = DataAccessDecision(agent_type="FinOps", dataset="hr", table="employees", denied=True, reason="out of scope")
    with pytest.raises(PermissionError, match="denied"):
        AwsLakeFormationEnforcer().scoped_query(denied, database="hr", table="employees")


def test_aws_fgac_apply_is_defense_in_depth():
    # As a DataAccessEnforcer it still masks post-fetch rows (delegates in-process).
    from adapters.aws.data_fgac import AwsLakeFormationEnforcer
    dec = _finops_decision()
    rows = [{"account_id": "a1", "cost_usd": 1, "region": "us-east-1", "customer_email": "x@y.com", "tax_id": "T-1"},
            {"account_id": "a2", "cost_usd": 2, "region": "eu-west-1", "customer_email": "z@y.com", "tax_id": "T-2"}]
    out = AwsLakeFormationEnforcer().apply(dec, rows)
    assert len(out) == 1 and out[0]["region"] == "us-east-1"
    assert out[0]["customer_email"] == "***REDACTED***" and out[0]["tax_id"] == "***REDACTED***"


def test_aws_fgac_register_filter_requires_boto3(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    from adapters.aws.data_fgac import AwsLakeFormationEnforcer
    with pytest.raises(RuntimeError, match="boto3"):
        AwsLakeFormationEnforcer().register_data_cells_filter(_finops_decision(), database="finops", table="billing")


def test_aws_fgac_satisfies_enforcer_protocol():
    from governance.extensions.data_fgac import DataAccessEnforcer
    from adapters.aws.data_fgac import AwsLakeFormationEnforcer
    assert isinstance(AwsLakeFormationEnforcer(), DataAccessEnforcer)
