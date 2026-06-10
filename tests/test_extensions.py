"""
tests/test_extensions.py — the WS7 gap modules (agnostic; always run).

Covers Gap 1 (data-layer FGAC), Gap 3 (data-access drift), Gap 4 (reasoning-step
validation), and Gap 4+ (CoT/CoVe trace logging). All cloud-neutral; no cloud SDK
or agent framework required. Feature flags are verified off-by-default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from governance.extensions import flags

_CATALOG = Path(__file__).parent.parent / "governance" / "extensions" / "configs" / "data-classification.example.yaml"


# ── Feature flags: all OFF by default ─────────────────────────────────────────

def test_flags_default_off(monkeypatch):
    for f in (flags.DATA_FGAC, flags.DATA_DRIFT, flags.REASONING_GUARD, flags.REASONING_TRACE):
        monkeypatch.delenv(f, raising=False)
        assert flags.is_enabled(f) is False


def test_flag_truthy_parsing(monkeypatch):
    monkeypatch.setenv(flags.DATA_FGAC, "true")
    assert flags.is_enabled(flags.DATA_FGAC) is True
    monkeypatch.setenv(flags.DATA_FGAC, "0")
    assert flags.is_enabled(flags.DATA_FGAC) is False


# ── Gap 1: data-layer FGAC ────────────────────────────────────────────────────

def _catalog():
    from governance.extensions.data_classification import DataClassificationCatalog
    return DataClassificationCatalog.load(_CATALOG)


def test_catalog_loads_msgk_types_from_env(monkeypatch):
    from governance.extensions.data_classification import (
        DataClassificationCatalog, DataClassification, DataLabel, ABACPolicy)
    monkeypatch.setenv("GALAXY_DATA_CLASSIFICATION_PATH", str(_CATALOG))
    cat = DataClassificationCatalog.load()  # no arg → resolves via env
    lbl = cat.label_for("finops", "billing", "tax_id")
    assert isinstance(lbl, DataLabel) and lbl.classification == DataClassification.RESTRICTED
    # unclassified column fails closed to RESTRICTED
    assert cat.label_for("finops", "billing", "nope").classification == DataClassification.RESTRICTED
    pols = cat.policies_for("FinOps")
    assert len(pols) == 1 and isinstance(pols[0], ABACPolicy)
    assert pols[0].max_classification == DataClassification.CONFIDENTIAL


def test_fgac_masks_and_filters_for_finops():
    from governance.extensions.data_fgac import DataAccessMediator
    med = DataAccessMediator(catalog=_catalog())
    rows = [
        {"account_id": "a1", "cost_usd": 10, "region": "us-east-1", "customer_email": "x@y.com", "tax_id": "T-1"},
        {"account_id": "a2", "cost_usd": 20, "region": "eu-west-1", "customer_email": "z@y.com", "tax_id": "T-2"},
    ]
    decision, out = med.read(
        agent_type="FinOps", dataset="finops", table="billing",
        columns=["account_id", "cost_usd", "region", "customer_email", "tax_id"], rows=rows,
    )
    assert decision.permitted
    # customer_email always masked; tax_id (RESTRICTED) above FinOps clearance (CONFIDENTIAL) → masked
    assert set(decision.masked_columns) == {"customer_email", "tax_id"}
    # row_filter keeps only us-east-1
    assert len(out) == 1
    assert out[0]["region"] == "us-east-1"
    assert out[0]["customer_email"] == "***REDACTED***"
    assert out[0]["tax_id"] == "***REDACTED***"
    assert out[0]["account_id"] == "a1"          # allowed, passes through


def test_fgac_finops_reading_hr_masks_by_category():
    # FinOps HAS a policy, but HR-category columns aren't in its allowed categories
    # → MSGK's evaluator denies them → masked (not a whole-request deny).
    from governance.extensions.data_fgac import DataAccessMediator
    med = DataAccessMediator(catalog=_catalog())
    d = med.authorize(agent_type="FinOps", dataset="hr", table="employees", columns=["employee_id", "salary"])
    assert d.permitted
    assert "salary" in d.masked_columns        # CONFIDENTIAL HR-category → not in FinOps scope → masked


def test_fgac_unknown_agent_is_deny_all():
    from governance.extensions.data_fgac import DataAccessMediator
    med = DataAccessMediator(catalog=_catalog())
    decision = med.authorize(agent_type="NoSuchAgent", dataset="finops", table="billing", columns=["cost_usd"])
    assert decision.denied


def test_fgac_authorize_for_identity_binds_nhi():
    from dataclasses import dataclass
    from governance.extensions.data_fgac import DataAccessMediator

    @dataclass
    class _Ident:
        agent_type: str
        client_id: str

    med = DataAccessMediator(catalog=_catalog())
    d = med.authorize_for_identity(
        identity=_Ident("FinOps", "cid-finops-1"), dataset="finops", table="billing", columns=["cost_usd"],
    )
    assert d.permitted
    assert d.nhi_id == "cid-finops-1"            # decision attributed to the principal


# ── Gap 3: data-access drift ──────────────────────────────────────────────────

def _drift():
    from governance.extensions.data_drift import DataAccessDriftDetector, InMemoryBaselineStore, DriftConfig
    return DataAccessDriftDetector(store=InMemoryBaselineStore(), config=DriftConfig(min_samples=3, z_threshold=2.0))


def test_drift_baseline_then_volume_spike():
    det = _drift()
    # establish a baseline of small reads on one table
    for _ in range(5):
        r = det.record_access(agent_type="FinOps", dataset="finops", table="billing", columns_read=2, max_sensitivity=1)
        assert r.quarantine_recommended is False
    # a sudden large read → volume spike
    r = det.record_access(agent_type="FinOps", dataset="finops", table="billing", columns_read=500, max_sensitivity=1)
    assert "volume_spike" in r.signals


def test_drift_first_seen_table_and_sensitivity_escalation():
    det = _drift()
    for _ in range(4):
        det.record_access(agent_type="FinOps", dataset="finops", table="billing", columns_read=2, max_sensitivity=1)
    r = det.record_access(agent_type="FinOps", dataset="hr", table="employees", columns_read=2, max_sensitivity=3)
    assert "new_table" in r.signals
    assert "sensitivity_escalation" in r.signals
    assert r.quarantine_recommended is True      # combined weight crosses the threshold


def test_drift_baseline_persists_across_instances(tmp_path):
    from governance.extensions.data_drift import DataAccessDriftDetector, JsonFileBaselineStore, DriftConfig
    path = tmp_path / "baselines.json"
    cfg = DriftConfig(min_samples=3, z_threshold=2.0)
    det1 = DataAccessDriftDetector(store=JsonFileBaselineStore(path), config=cfg)
    for _ in range(5):
        det1.record_access(agent_type="FinOps", dataset="finops", table="billing", columns_read=2, max_sensitivity=1)
    # a fresh detector (cold start) reads the persisted baseline → still detects the spike
    det2 = DataAccessDriftDetector(store=JsonFileBaselineStore(path), config=cfg)
    assert det2.baseline("FinOps")["access_count"] == 5
    r = det2.record_access(agent_type="FinOps", dataset="finops", table="billing", columns_read=500, max_sensitivity=1)
    assert "volume_spike" in r.signals


# ── Gap 4: reasoning-step validation ──────────────────────────────────────────

def test_reasoning_guard_denies_unlisted_tool():
    from governance.extensions.reasoning_guard import ReasoningStepValidator, ReasoningStep
    v = ReasoningStepValidator()
    plan = v.validate_plan(
        agent_type="Analyzer",
        steps=[ReasoningStep(kind="tool_call", tool="shell_exec")],
        allowed_tools=set(),                     # analyzer has no tools
    )
    assert plan.allowed is False
    assert "capability_violation" in plan.first_denial.signals


def test_reasoning_guard_allows_listed_tool():
    from governance.extensions.reasoning_guard import ReasoningStepValidator, ReasoningStep
    v = ReasoningStepValidator()
    plan = v.validate_plan(
        agent_type="Coder", steps=[ReasoningStep(kind="tool_call", tool="read_file")],
        allowed_tools={"read_file"},
    )
    assert plan.allowed is True


def test_reasoning_guard_denies_out_of_scope_data_access():
    from governance.extensions.data_fgac import DataAccessMediator
    from governance.extensions.reasoning_guard import ReasoningStepValidator, ReasoningStep
    v = ReasoningStepValidator(mediator=DataAccessMediator(catalog=_catalog()))
    plan = v.validate_plan(
        agent_type="Intruder",   # no ABAC policy → mediator denies the whole request
        steps=[ReasoningStep(kind="data_access", dataset="finops", table="billing", columns=("tax_id",))],
        allowed_tools=set(),
    )
    assert plan.allowed is False
    assert "data_out_of_scope" in plan.first_denial.signals


# ── Cedar standards-based authz (policy_engine) ───────────────────────────────

def test_cedar_authorizer_permit_all():
    from governance.extensions.policy_engine import CedarAuthorizer
    auth = CedarAuthorizer(policy_content="permit(principal, action, resource);")
    assert auth.authorize_action(principal="FinOps", action="use_tool", resource="read_file") is True


def test_cedar_authorizer_fail_closed():
    from governance.extensions.policy_engine import CedarAuthorizer
    auth = CedarAuthorizer(policy_content="forbid(principal, action, resource);")
    assert auth.authorize_action(principal="FinOps", action="use_tool", resource="rm_rf") is False


def test_build_authorizer_flag_gated(monkeypatch):
    from governance.extensions import policy_engine as pe
    monkeypatch.delenv(pe.POLICY_ENGINE_ENV, raising=False)
    assert pe.build_authorizer() is None              # off by default
    monkeypatch.setenv(pe.POLICY_ENGINE_ENV, "cedar")
    assert pe.build_authorizer() is not None           # loads the bundled authz.cedar


def test_reasoning_guard_uses_cedar_when_wired():
    from governance.extensions.policy_engine import CedarAuthorizer
    from governance.extensions.reasoning_guard import ReasoningStepValidator, ReasoningStep
    # With Cedar wired, the engine is the tool-authz decision point (overrides the allow-list).
    permit = ReasoningStepValidator(authorizer=CedarAuthorizer(policy_content="permit(principal, action, resource);"))
    assert permit.validate_plan(agent_type="X", steps=[ReasoningStep(kind="tool_call", tool="anything")],
                                allowed_tools=set()).allowed is True
    forbid = ReasoningStepValidator(authorizer=CedarAuthorizer(policy_content="forbid(principal, action, resource);"))
    assert forbid.validate_plan(agent_type="X", steps=[ReasoningStep(kind="tool_call", tool="anything")],
                                allowed_tools={"anything"}).allowed is False


def test_cedar_conditional_abac_full_engine():
    # Conditional ABAC (when {...}) needs the real Cedar engine; skip if absent.
    pytest.importorskip("cedarpy")
    from governance.extensions.policy_engine import CedarAuthorizer
    from governance.extensions.data_classification import DataClassification, DataLabel
    auth = CedarAuthorizer()  # bundled authz.cedar
    # RESTRICTED (>=3) denied for non-Auditor
    assert auth.authorize_data(agent_type="FinOps", dataset="finops", table="billing", column="tax_id",
                               label=DataLabel(classification=DataClassification.RESTRICTED, categories=["PII"])) is False


# ── Gap 4+: CoT/CoVe trace logging (mandatory redaction) ──────────────────────

class _FakeRedactor:
    """Stand-in matching the CredentialRedactor surface used by the logger."""
    def redact(self, value):
        return (value or "").replace("sk-SECRET123", "[REDACTED]")
    def contains_pii(self, value):
        return False
    def find_pii_matches(self, value):
        return []


def test_reasoning_trace_redacts_before_logging():
    from governance.extensions.reasoning_trace import ReasoningTraceLogger
    logger = ReasoningTraceLogger(redactor=_FakeRedactor())
    rec = logger.capture(
        run_id="run-1", agent_type="Analyzer", nhi_id="cid-1",
        cot="I will call the API with key sk-SECRET123 to fetch data.",
        cove="Q: is the key valid? A: sk-SECRET123 looks valid.",
        decision="allow",
    )
    assert rec is not None
    assert "sk-SECRET123" not in rec.cot          # credential never reaches the sink
    assert "sk-SECRET123" not in rec.cove
    assert "[REDACTED]" in rec.cot
    assert rec.redaction_applied is True
    assert rec.cot_hash and rec.cove_hash


def test_reasoning_trace_writes_audit_record():
    from governance.extensions.reasoning_trace import ReasoningTraceLogger

    captured = []

    class _Backend:
        def write(self, entry):
            captured.append(entry)
        def flush(self):
            pass

    logger = ReasoningTraceLogger(audit_backend=_Backend(), redactor=_FakeRedactor())
    logger.capture(run_id="run-1", agent_type="Analyzer", nhi_id="cid-1",
                   cot="benign reasoning", decision="deny")
    assert len(captured) == 1
    assert captured[0].event_type == "reasoning_trace"
    assert captured[0].agent_id == "cid-1"
    assert captured[0].metadata["redaction_applied"] in (True, False)


def test_reasoning_trace_requires_a_redactor(monkeypatch):
    # Mandatory redaction: with no redactor and MSGK's unavailable, refuse to run.
    import sys
    monkeypatch.setitem(sys.modules, "agent_os.credential_redactor", None)
    from governance.extensions.reasoning_trace import ReasoningTraceLogger
    with pytest.raises(RuntimeError, match="mandatory redaction"):
        ReasoningTraceLogger()
