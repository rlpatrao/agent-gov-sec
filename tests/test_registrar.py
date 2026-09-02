"""Tests for the Governance Authority control plane: agent-type discovery, the
identity binding store, and the Registrar's two-key model.

The security properties under test, in order of importance:
  1. Enrollment binds identity only — it never grants a capability, and an
     enrolled agent with no approved policy is still denied at the chokepoint.
  2. An agent cannot enroll: a caller whose principal is already bound to an
     agent type is refused, as is any non-human principal.
  3. A silent identity rebind is impossible without an explicit rotate.
  4. The control plane is closed unless a token is configured.
"""

from __future__ import annotations

import json

import pytest

from galaxy_gov.policy_export import discover_agent_types
from galaxy_gov.remote.identity_store import (
    STATUS_REVOKED,
    IdentityBinding,
    IdentityStore,
    IdentityStoreError,
)
from galaxy_gov.remote.registrar import (
    STATUS_PENDING_POLICY,
    STATUS_READY,
    CallerIdentity,
    EnrollmentDenied,
    VerifiedPrincipal,
    authorize_enroller,
    enroll,
    resolve_identity,
)

ACCOUNT = "111122223333"
SSO_CALLER = CallerIdentity(
    arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Dev_abc123/alice@example.com",
    account=ACCOUNT,
)
AGENT_ROLE = f"arn:aws:iam::{ACCOUNT}:role/galaxy-rp-FinOps"


class FakeVerifier:
    """PrincipalVerifier double. `known` lists role ARNs that 'exist' in the cloud."""

    def __init__(self, caller: CallerIdentity = SSO_CALLER, known: tuple[str, ...] = (AGENT_ROLE,)):
        self._caller = caller
        self._known = known

    def caller_identity(self) -> CallerIdentity:
        return self._caller

    def verify_principal(self, *, agent_type: str, principal_id: str) -> VerifiedPrincipal:
        if principal_id not in self._known:
            raise EnrollmentDenied(f"IAM role {principal_id!r} does not exist")
        return VerifiedPrincipal(principal_id=principal_id, account=ACCOUNT, cloud="aws")


@pytest.fixture
def store(tmp_path) -> IdentityStore:
    return IdentityStore(tmp_path / "identity-bindings.yaml")


def _registry_with(*agent_types: str) -> dict:
    return {"version": "1.0", "default": "deny",
            "agents": {at: {"agent_type": at, "allowed_tools": []} for at in agent_types}}


# ── Phase 1: discovery replaces the hand-maintained tuple ────────────────────

def test_discovery_reads_agent_type_from_each_config():
    """The filesystem is the source of truth; discovery must find every config."""
    types = discover_agent_types()
    assert set(types) >= {"FinOps", "Auditor", "Rogue"}
    assert types == tuple(sorted(types)), "discovery must be deterministic"


def test_discovery_of_a_new_config_needs_no_code_change(tmp_path):
    (tmp_path / "payroll.yaml").write_text(
        "agent:\n  type: Payroll\n  prompt_file: prompts/payroll.md\n", encoding="utf-8")
    (tmp_path / "ledger.yaml").write_text(
        "agent:\n  type: Ledger\n  prompt_file: prompts/ledger.md\n", encoding="utf-8")
    assert discover_agent_types(tmp_path) == ("Ledger", "Payroll")


def test_discovery_skips_unreadable_and_typeless_configs(tmp_path):
    (tmp_path / "good.yaml").write_text("agent:\n  type: Good\n", encoding="utf-8")
    (tmp_path / "no_type.yaml").write_text("agent:\n  description: nothing\n", encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("agent: [unclosed\n", encoding="utf-8")
    assert discover_agent_types(tmp_path) == ("Good",)


def test_discovery_of_missing_directory_is_empty_not_permissive(tmp_path):
    """Empty must mean 'nothing to export', never 'allow everything'."""
    assert discover_agent_types(tmp_path / "absent") == ()


# ── The identity store ───────────────────────────────────────────────────────

def test_store_round_trip_and_idempotence(store):
    binding = IdentityBinding(agent_type="FinOps", principal_id=AGENT_ROLE,
                              cloud="aws", account=ACCOUNT, enrolled_by=SSO_CALLER.arn)
    first = store.put(binding)
    assert first.enrolled_at, "the store stamps an enrollment time"
    assert store.put(binding).enrolled_at == first.enrolled_at, "re-put must not re-stamp"
    assert store.get("FinOps").principal_id == AGENT_ROLE


def test_store_refuses_silent_rebind(store):
    store.put(IdentityBinding(agent_type="FinOps", principal_id=AGENT_ROLE, cloud="aws"))
    hijack = IdentityBinding(agent_type="FinOps", cloud="aws",
                             principal_id=f"arn:aws:iam::{ACCOUNT}:role/attacker")
    with pytest.raises(IdentityStoreError, match="already bound"):
        store.put(hijack)
    assert store.get("FinOps").principal_id == AGENT_ROLE, "the original binding survives"
    assert store.put(hijack, allow_rotate=True).principal_id.endswith("attacker")


def test_revoked_binding_stops_resolving_but_is_retained(store):
    store.put(IdentityBinding(agent_type="FinOps", principal_id=AGENT_ROLE, cloud="aws"))
    assert store.revoke("FinOps").status == STATUS_REVOKED
    assert store.get("FinOps") is None, "a revoked identity must not resolve"
    assert store.load()["FinOps"].status == STATUS_REVOKED, "the audit record is kept"


def test_unparseable_store_denies_reads_and_refuses_writes(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("bindings: [not, a, mapping]\n", encoding="utf-8")
    bad = IdentityStore(path)
    assert bad.get("FinOps") is None, "a corrupt store must fail closed, not raise"
    with pytest.raises(IdentityStoreError):
        bad.put(IdentityBinding(agent_type="FinOps", principal_id=AGENT_ROLE, cloud="aws"))
    assert path.read_text(encoding="utf-8") == "bindings: [not, a, mapping]\n", "not clobbered"


# ── The two-key model ────────────────────────────────────────────────────────

def test_enrollment_alone_does_not_authorize(store):
    """Key 1 without key 2: the identity resolves, the agent is still denied."""
    result = enroll("Payroll", AGENT_ROLE, verifier=FakeVerifier(known=(AGENT_ROLE,)),
                    store=store, registry=_registry_with("FinOps"))
    assert result.status == STATUS_PENDING_POLICY
    assert "403" in result.message and "no_governance_policy" in result.message
    assert store.get("Payroll") is not None, "identity is bound"

    from galaxy_gov.remote import enforce
    session = enforce.session_for("Payroll", _registry_with("FinOps"), nhi_id=AGENT_ROLE)
    assert session is None, "an enrolled agent with no policy still resolves to no session"


def test_enrollment_with_approved_policy_is_ready(store):
    result = enroll("FinOps", AGENT_ROLE, verifier=FakeVerifier(),
                    store=store, registry=_registry_with("FinOps"))
    assert result.status == STATUS_READY
    assert result.binding.principal_id == AGENT_ROLE
    assert result.binding.enrolled_by == SSO_CALLER.arn, "the human actor is recorded"


def test_enrollment_records_no_capabilities(store):
    """The binding carries identity only — no field can express a tool grant."""
    result = enroll("FinOps", AGENT_ROLE, verifier=FakeVerifier(),
                    store=store, registry=_registry_with("FinOps"))
    fields = set(result.binding.to_dict())
    assert not fields & {"allowed_tools", "denied_tools", "allowed_recipients", "model_boundary"}


# ── Enroller authorization ───────────────────────────────────────────────────

def test_an_agent_cannot_enroll(store):
    """The decisive check: a caller already bound to an agent type is refused."""
    store.put(IdentityBinding(agent_type="FinOps", principal_id=AGENT_ROLE, cloud="aws"))
    agent_caller = CallerIdentity(
        arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/galaxy-rp-FinOps/session-1", account=ACCOUNT)
    with pytest.raises(EnrollmentDenied, match="agents may not enroll"):
        enroll("Payroll", AGENT_ROLE, verifier=FakeVerifier(caller=agent_caller),
               store=store, registry={})


def test_non_human_principal_is_refused(store):
    task_role = CallerIdentity(
        arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/some-ecs-task/session", account=ACCOUNT)
    with pytest.raises(EnrollmentDenied, match="not a human principal"):
        authorize_enroller(task_role, store)


def test_ci_role_is_allowed_when_explicitly_listed(store, monkeypatch):
    ci = CallerIdentity(arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/gh-actions/run-7",
                        account=ACCOUNT)
    with pytest.raises(EnrollmentDenied):
        authorize_enroller(ci, store)
    monkeypatch.setenv("GOV_ENROLL_ALLOWED_ROLES", "gh-actions,other")
    authorize_enroller(ci, store)  # no raise


def test_iam_user_and_sso_session_are_accepted(store):
    authorize_enroller(SSO_CALLER, store)
    authorize_enroller(
        CallerIdentity(arn=f"arn:aws:iam::{ACCOUNT}:user/alice", account=ACCOUNT), store)


# ── Input validation and verification ────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "not-pascal", "has space", "9Leading", "Semi;colon"])
def test_malformed_agent_type_is_refused(store, bad):
    with pytest.raises(EnrollmentDenied, match="invalid agent type"):
        enroll(bad, AGENT_ROLE, verifier=FakeVerifier(), store=store, registry={})


def test_nonexistent_principal_is_refused(store):
    with pytest.raises(EnrollmentDenied, match="does not exist"):
        enroll("FinOps", f"arn:aws:iam::{ACCOUNT}:role/ghost",
               verifier=FakeVerifier(known=(AGENT_ROLE,)), store=store, registry={})
    assert store.get("FinOps") is None, "a failed verification records nothing"


def test_resolve_identity_is_fail_closed(store):
    assert resolve_identity("Unknown", store=store, registry={}) is None
    enroll("FinOps", AGENT_ROLE, verifier=FakeVerifier(), store=store,
           registry=_registry_with("FinOps"))
    resolved = resolve_identity("FinOps", store=store, registry=_registry_with("FinOps"))
    assert resolved["principal_id"] == AGENT_ROLE
    assert resolved["status"] == STATUS_READY


# ── Control-plane gating on the server ───────────────────────────────────────

def test_control_plane_is_closed_without_a_token(monkeypatch):
    from galaxy_gov.remote import server
    monkeypatch.delenv("GOV_CONTROL_TOKEN", raising=False)
    assert server._control_authorized({"authorization": "Bearer anything"}) is False


def test_control_plane_requires_the_exact_token(monkeypatch):
    from galaxy_gov.remote import server
    monkeypatch.setenv("GOV_CONTROL_TOKEN", "s3cret")
    assert server._control_authorized({"authorization": "Bearer s3cret"}) is True
    assert server._control_authorized({"authorization": "Bearer wrong"}) is False
    assert server._control_authorized({}) is False


def test_unverified_caller_header_is_not_trusted_by_default(monkeypatch):
    from galaxy_gov.remote import server
    monkeypatch.delenv("GOV_CONTROL_TRUST_HEADER", raising=False)
    with pytest.raises(EnrollmentDenied, match="not trusted"):
        server._caller_identity_from_headers({"x-amzn-iam-caller-arn": SSO_CALLER.arn})
    with pytest.raises(EnrollmentDenied, match="no verified caller identity"):
        server._caller_identity_from_headers({})
    monkeypatch.setenv("GOV_CONTROL_TRUST_HEADER", "1")
    assert server._caller_identity_from_headers(
        {"x-amzn-iam-caller-arn": SSO_CALLER.arn}).account == ACCOUNT


def test_registry_digest_exposes_coverage_not_policy(monkeypatch):
    from galaxy_gov.remote import server
    monkeypatch.setenv("GOV_POLICY_REGISTRY", json.dumps(_registry_with("FinOps", "Auditor")))
    digest = server._registry_digest()
    assert digest["agent_types"] == ["Auditor", "FinOps"]
    assert digest["digest"].startswith("sha256:")
    assert "allowed_tools" not in json.dumps(digest), "must not leak policy content"


# ── End-to-end: NHI resolution against a live authority ──────────────────────

@pytest.fixture
def live_authority(tmp_path, monkeypatch):
    """Run the real server on an ephemeral port with a seeded identity store."""
    import threading
    from http.server import ThreadingHTTPServer

    store_path = tmp_path / "bindings.yaml"
    IdentityStore(store_path).put(
        IdentityBinding(agent_type="FinOps", principal_id=AGENT_ROLE, cloud="aws",
                        account=ACCOUNT, enrolled_by=SSO_CALLER.arn))
    monkeypatch.setenv("GOV_IDENTITY_STORE", str(store_path))
    monkeypatch.setenv("GOV_CONTROL_TOKEN", "test-token")
    monkeypatch.setenv("GOV_POLICY_REGISTRY", json.dumps(_registry_with("FinOps")))

    from galaxy_gov.remote import server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_health_reports_control_plane_state(live_authority):
    import urllib.request
    with urllib.request.urlopen(f"{live_authority}/health", timeout=5) as r:
        assert json.loads(r.read())["control_plane"] == "enabled"


def test_identity_route_requires_the_control_token(live_authority):
    import urllib.error
    import urllib.request

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{live_authority}/identity?agent_type=FinOps", timeout=5)
    assert exc.value.code == 403

    req = urllib.request.Request(f"{live_authority}/identity?agent_type=FinOps")
    req.add_header("authorization", "Bearer test-token")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read())["principal_id"] == AGENT_ROLE


def test_nhi_resolves_from_the_authority(live_authority, monkeypatch):
    monkeypatch.setenv("GOV_AUTHORITY_ENDPOINT", live_authority)
    monkeypatch.setenv("GOV_AUTHORITY_TOKEN", "test-token")
    from core.nhi_registry import NHIRegistry
    assert NHIRegistry.get("FinOps").client_id == AGENT_ROLE


def test_env_bridge_cannot_bypass_a_configured_authority(live_authority, monkeypatch):
    """The property that makes the authority authoritative: an agent that sets its
    own NHI_CLIENT_ID_* must not be able to self-assert an identity the authority
    has no binding for."""
    monkeypatch.setenv("GOV_AUTHORITY_ENDPOINT", live_authority)
    monkeypatch.setenv("GOV_AUTHORITY_TOKEN", "test-token")
    monkeypatch.setenv("NHI_CLIENT_ID_PAYROLL", "self-asserted-identity")

    from core.nhi_registry import NHIRegistry
    with pytest.raises(ValueError, match="deliberately not"):
        NHIRegistry.get("Payroll")


def test_env_bridge_still_works_without_an_authority(monkeypatch):
    """Backwards compatibility: local development with no authority configured."""
    monkeypatch.delenv("GOV_AUTHORITY_ENDPOINT", raising=False)
    monkeypatch.setenv("NHI_CLIENT_ID_PAYROLL", "11111111-2222-3333-4444-555555555555")
    from core.nhi_registry import NHIRegistry
    assert NHIRegistry.get("Payroll").client_id.startswith("11111111")


def test_unreachable_authority_denies_rather_than_degrading(monkeypatch):
    monkeypatch.setenv("GOV_AUTHORITY_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("GOV_AUTHORITY_TIMEOUT", "1")
    monkeypatch.setenv("NHI_CLIENT_ID_FINOPS", "fallback-should-not-be-used")
    from core.nhi_registry import NHIRegistry
    with pytest.raises(ValueError):
        NHIRegistry.get("FinOps")
