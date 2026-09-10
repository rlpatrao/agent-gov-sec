"""Tests for the client-side package, galaxy_agentkit.

The theme is that the kit refuses to run an agent it cannot govern. Each test
here pins one refusal, because the failure mode being defended against is not a
crash — it is an agent that starts, looks governed, and is not.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from galaxy_agentkit import (
    ConfigurationError,
    EnforcementDenied,
    EnforcementUnavailable,
    Settings,
    govern,
)
from galaxy_agentkit.config import REQUIRED_CONFIGS, missing_configs, verify_bundle

BASE_ENV = {
    "GALAXY_AGENT_TYPE": "FinOps",
    "GALAXY_NHI_ID": "local-finops-nhi",
    "GALAXY_ENFORCEMENT_ENDPOINT": "http://127.0.0.1:9",
    "GALAXY_TIMEOUT_SECONDS": "2",
}


# ── settings ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "missing,env",
    [
        ("GALAXY_AGENT_TYPE", {k: v for k, v in BASE_ENV.items() if k != "GALAXY_AGENT_TYPE"}),
        ("GALAXY_NHI_ID", {k: v for k, v in BASE_ENV.items() if k != "GALAXY_NHI_ID"}),
        (
            "GALAXY_ENFORCEMENT_ENDPOINT",
            {k: v for k, v in BASE_ENV.items() if k != "GALAXY_ENFORCEMENT_ENDPOINT"},
        ),
    ],
)
def test_settings_reject_incomplete_environment(missing, env):
    with pytest.raises(ConfigurationError) as exc:
        Settings.from_env(env)
    assert missing in str(exc.value)


def test_settings_reject_relative_endpoint():
    env = dict(BASE_ENV, GALAXY_ENFORCEMENT_ENDPOINT="localhost:8080")
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)


def test_settings_reject_unknown_mode():
    with pytest.raises(ConfigurationError):
        Settings.from_env(dict(BASE_ENV, GALAXY_MODE="advisory"))


def test_inprocess_mode_needs_no_endpoint():
    """In-process is defence in depth and legitimately has no authority URL."""
    env = {k: v for k, v in BASE_ENV.items() if k != "GALAXY_ENFORCEMENT_ENDPOINT"}
    settings = Settings.from_env(dict(env, GALAXY_MODE="inprocess"))
    assert settings.calls_inprocess and not settings.calls_remote


def test_route_building():
    assert Settings.from_env(BASE_ENV).route("llm") == "http://127.0.0.1:9/llm"


# ── configuration bundle ─────────────────────────────────────────────────
def test_config_bundle_is_complete():
    """Every file the guards read must be present; this is the packaging gate."""
    assert missing_configs() == [], (
        "governance configuration is missing from the install: "
        f"{[c.relative_path for c in missing_configs()]}"
    )
    assert verify_bundle().is_dir()
    assert len(REQUIRED_CONFIGS) >= 8


def test_pipeline_refuses_to_build_without_injection_rules(monkeypatch, tmp_path):
    """The core regression: a missing rule file must raise, not fall back.

    `PromptInjectionDetector(None)` silently uses the toolkit's sample rules, so
    a packaging mistake would downgrade the control while still reporting
    success. Pin the loud failure.
    """
    from galaxy_gov.shared.enforcement import pipeline

    monkeypatch.setattr(
        pipeline, "_PROMPT_INJECTION_CONFIG", tmp_path / "absent.yaml"
    )
    with pytest.raises(pipeline.GovernanceConfigError) as exc:
        pipeline._build_injection_detector()
    assert "sample rules" in str(exc.value)


# ── client behaviour against a stub authority ────────────────────────────
class _StubAuthority(BaseHTTPRequestHandler):
    """Minimal stand-in speaking the real chokepoint contract."""

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"status": "ok", "service": "stub", "version": "9.9.9"})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        # Identity must arrive in headers, never in the body.
        assert self.headers.get("x-agent-type") == "FinOps"
        assert self.headers.get("x-nhi-id") == "local-finops-nhi"
        if body.get("recipient") == "Rogue":
            self._send(403, {"error": "recipient_not_allowed", "reason": "denied"})
        elif self.path.rstrip("/") == "/llm":
            self._send(200, {"output": {"message": {"content": [{"text": "ok"}]}}})
        else:
            self._send(200, {"decision": "allow"})


@pytest.fixture
def authority():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubAuthority)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _agent(authority_url):
    return govern(settings=Settings.from_env(dict(BASE_ENV, GALAXY_ENFORCEMENT_ENDPOINT=authority_url)))


def test_connects_and_records_authority_version(authority):
    agent = _agent(authority)
    assert agent.authority["version"] == "9.9.9"


def test_allow_path(authority):
    assert _agent(authority).a2a("Auditor") == {"decision": "allow"}


def test_denial_raises_with_the_control_code(authority):
    with pytest.raises(EnforcementDenied) as exc:
        _agent(authority).a2a("Rogue")
    assert exc.value.code == "recipient_not_allowed"
    assert exc.value.route == "a2a"


def test_llm_round_trip(authority):
    out = _agent(authority).llm([{"role": "user", "content": [{"text": "hi"}]}])
    assert out["output"]["message"]["content"][0]["text"] == "ok"


def test_unreachable_authority_refuses_to_start():
    """Fail closed: an unreachable authority is not permission to run."""
    with pytest.raises(EnforcementUnavailable):
        govern(settings=Settings.from_env(BASE_ENV))


# ── `galaxy init` scaffolder ─────────────────────────────────────────────
def test_scaffold_generates_a_complete_project(tmp_path):
    from galaxy_gov.tooling.scaffold import init

    assert init(["payroll-agent", "--root", str(tmp_path)]) == 0
    root = tmp_path / "payroll-agent"
    for rel in (
        ".env.example",
        "README.md",
        "config/payroll-agent.yaml",
        "config/data-classification.yaml",
        "prompts/payroll-agent.md",
        "src/payroll_agent/agent.py",
        "tests/test_payroll_agent.py",
    ):
        assert (root / rel).is_file(), f"scaffold did not create {rel}"

    import yaml

    config = yaml.safe_load((root / "config/payroll-agent.yaml").read_text())
    assert config["agent"]["type"] == "Payroll"
    # Floor-safe defaults: the generated request must not start weakened.
    assert config["governance"]["enable_prompt_injection_guard"] is True
    assert config["governance"]["allowed_tools"] == []


def test_scaffold_refuses_to_overwrite(tmp_path):
    from galaxy_gov.tooling.scaffold import init

    assert init(["payroll-agent", "--root", str(tmp_path)]) == 0
    assert init(["payroll-agent", "--root", str(tmp_path)]) == 1
    assert init(["payroll-agent", "--root", str(tmp_path), "--force"]) == 0
