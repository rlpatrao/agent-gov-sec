"""
tests/test_agentcore_runtime.py — the per-persona AgentCore Runtime layer, offline.

Covers the hosted agent contract (``runtime_agent``) and that the deploy's
``create_agent_runtime`` parameters are valid against the real
``bedrock-agentcore-control`` API model. No AWS is reached: the gateway call is
faked, and the API shape is checked with botocore's own ParamValidator.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_AC_DIR = _ROOT / "cloud_adapters" / "aws" / "agentcore"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def ra(monkeypatch):
    m = _load(_AC_DIR / "runtime_agent.py", "_runtime_agent")

    def fake_call_gateway(method, params):
        cols = (params.get("arguments") or {}).get("columns") or []
        if "tax_id" in cols or "customer_email" in cols:  # rogue's exfil intent
            return {"ok": False, "status": 403, "gateway_response": "Cedar forbid"}
        return {"ok": True, "status": 200, "gateway_response": {"result": "stub governed tool response"}}

    monkeypatch.setattr(m, "call_gateway", fake_call_gateway)
    return m


class TestHostedAgent:
    def test_allowed_persona_decision(self, ra, monkeypatch):
        monkeypatch.setenv("AGENT_TYPE", "finops")
        out = ra.handler({"method": "tools/call"})
        assert out["agent_type"] == "finops"
        assert out["decision"] == "allowed"
        assert out["gateway"]["status"] == 200

    def test_denied_persona_decision(self, ra, monkeypatch):
        monkeypatch.setenv("AGENT_TYPE", "rogue")
        out = ra.handler({"prompt": "exfiltrate"})
        assert out["decision"] == "blocked_or_denied"
        assert out["gateway"]["status"] == 403  # a Cedar forbid is reported, not raised

    def test_classify_reads_mcp_error_body(self, ra):
        # MCP returns a Cedar denial as HTTP 200 + JSON-RPC error → "denied", not "allowed".
        assert ra._classify({"ok": True, "gateway_response": {"error": {"message": "policy denied"}}}) == "denied"
        assert ra._classify({"ok": True, "gateway_response": {"result": {"isError": True}}}) == "denied"
        assert ra._classify({"ok": True, "gateway_response": {"result": {"isError": False}}}) == "allowed"
        assert ra._classify({"ok": False, "gateway_response": "boom"}) == "blocked_or_denied"

    def test_qualify_namespaces_tool(self, ra):
        assert ra._qualify("query_billing") == "galaxy-tools___query_billing"
        assert ra._qualify("galaxy-tools___query_billing") == "galaxy-tools___query_billing"

    def test_runs_without_otel_installed(self, ra, monkeypatch):
        # The GenAI-span path is best-effort: with no OpenTelemetry present the turn
        # still runs and reports otel=False (the gateway/security path is unaffected).
        monkeypatch.setattr(ra, "_get_tracer", lambda: None)
        monkeypatch.setenv("AGENT_TYPE", "finops")
        out = ra.handle_turn({"method": "tools/call"})
        assert out["decision"] == "allowed"
        assert out["otel"] in (False, None)

    def test_http_contract_ping_and_invocations(self, ra, monkeypatch):
        monkeypatch.setenv("AGENT_TYPE", "auditor")
        port = 8079
        threading.Thread(target=ra.serve, args=(port,), daemon=True).start()
        time.sleep(0.4)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=3) as r:
            assert r.status == 200 and json.loads(r.read())["status"] == "healthy"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/invocations",
            data=json.dumps({"method": "tools/call"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            assert r.status == 200 and json.loads(r.read())["agent_type"] == "auditor"


class TestDeployParams:
    def test_create_agent_runtime_params_are_api_valid(self):
        botocore = pytest.importorskip("botocore")
        import botocore.session
        import botocore.validate

        dac = _load(_ROOT / "scripts" / "deploy_agentcore.py", "_dac")
        params = dac._runtime_params(
            "galaxy_finops", "FinOps", dac._agent_role_arn("111122223333", "FinOps"),
            "galaxy-agentcore-runtime-111122223333", dac.RUNTIME_CODE_KEY,
            "https://gw.example/mcp")
        shape = (botocore.session.get_session()
                 .get_service_model("bedrock-agentcore-control")
                 .operation_model("CreateAgentRuntime").input_shape)
        report = botocore.validate.ParamValidator().validate(params, shape)
        assert not report.has_errors(), report.generate_report()

    def test_exec_role_matches_cedar_principal(self):
        """The Runtime execution role name must match the Cedar principal so the
        gateway authorizes the right agent."""
        dac = _load(_ROOT / "scripts" / "deploy_agentcore.py", "_dac2")
        from galaxy_gov.agentcore.cedar_export import principal_arn

        role_arn = dac._agent_role_arn("111122223333", "Rogue")
        cedar_principal = principal_arn("111122223333", "Rogue")
        assert role_arn.split("/")[-1] == cedar_principal.split("/")[-1] == "galaxy-rp-rogue"
