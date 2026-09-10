"""
tests/test_dashboard.py — the Governance Dashboard and its decision buffer.

Covers the ring buffer (thread safety, eviction, counters that survive eviction),
control-code extraction from a denial body, the compiled crosswalk and its
staleness gate, and the served page end to end against the real server.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from galaxy_gov.remote._crosswalk import CROSSWALK, by_code
from galaxy_gov.remote.dashboard import render_dashboard
from galaxy_gov.remote.decision_log import (
    ALLOW,
    DENY,
    ERROR,
    DecisionLog,
    classify,
    control_code,
)

_ROOT = Path(__file__).resolve().parent.parent


# ── Ring buffer ──────────────────────────────────────────────────────────────

class TestDecisionLog:
    def test_records_only_metadata_fields(self):
        log = DecisionLog(capacity=8)
        record = log.record(route="/llm", agent_type="FinOps", nhi_id="nhi-1",
                            status=200, payload={"content": "secret prompt text"})
        assert set(record) == {"timestamp", "route", "agent_type", "nhi_id",
                               "outcome", "control", "status"}
        assert "secret prompt text" not in json.dumps(record)

    def test_missing_identity_fields_render_as_placeholders(self):
        log = DecisionLog(capacity=4)
        record = log.record(route="/a2a", agent_type=None, nhi_id="  ", status=200)
        assert record["agent_type"] == "-" and record["nhi_id"] == "-"

    def test_eviction_bounds_the_detail_but_not_the_counters(self):
        log = DecisionLog(capacity=5)
        for _ in range(50):
            log.record(route="/llm", agent_type="FinOps", nhi_id="nhi-1", status=200)
        assert log.buffered() == 5
        assert log.total() == 50
        assert log.route_counts()[("/llm", ALLOW)] == 50

    def test_recent_is_newest_first_and_limited(self):
        log = DecisionLog(capacity=100)
        for i in range(10):
            log.record(route=f"/r{i}", agent_type="A", nhi_id="n", status=200)
        recent = log.recent(limit=3)
        assert [r["route"] for r in recent] == ["/r9", "/r8", "/r7"]

    def test_counters_split_allow_deny_and_error(self):
        log = DecisionLog(capacity=100)
        log.record(route="/llm", agent_type="A", nhi_id="n", status=200)
        log.record(route="/llm", agent_type="A", nhi_id="n", status=403,
                   payload={"error": "B1", "reason": "prompt injection"})
        log.record(route="/llm", agent_type="A", nhi_id="n", status=502,
                   payload={"error": "upstream"})
        routes = log.route_counts()
        assert routes[("/llm", ALLOW)] == 1
        assert routes[("/llm", DENY)] == 1
        assert routes[("/llm", ERROR)] == 1
        assert log.control_counts()[("B1", DENY)] == 1

    def test_concurrent_writers_lose_no_records(self):
        log = DecisionLog(capacity=10_000)
        errors: list[BaseException] = []

        def writer(tag: str) -> None:
            try:
                for _ in range(250):
                    log.record(route="/data", agent_type=tag, nhi_id="n", status=200)
            except BaseException as exc:  # noqa: BLE001 — surfaced in the assertion
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(f"agent-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert log.total() == 2000
        assert log.buffered() == 2000
        assert log.route_counts()[("/data", ALLOW)] == 2000

    def test_concurrent_readers_see_a_consistent_snapshot(self):
        log = DecisionLog(capacity=64)
        stop = threading.Event()
        failures: list[str] = []

        def reader() -> None:
            while not stop.is_set():
                snapshot = log.recent(limit=64)
                if any(set(r) != {"timestamp", "route", "agent_type", "nhi_id",
                                  "outcome", "control", "status"} for r in snapshot):
                    failures.append("malformed record observed")

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        for _ in range(2000):
            log.record(route="/a2a", agent_type="FinOps", nhi_id="n", status=403,
                       payload={"error": "recipient_not_allowed"})
        stop.set()
        for t in readers:
            t.join()

        assert failures == []
        assert log.control_counts()[("recipient_not_allowed", DENY)] == 2000

    def test_reset_clears_records_and_counters(self):
        log = DecisionLog(capacity=8)
        log.record(route="/llm", agent_type="A", nhi_id="n", status=200)
        log.reset()
        assert log.total() == 0 and log.buffered() == 0 and log.route_counts() == {}

    def test_capacity_from_environment(self, monkeypatch):
        monkeypatch.setenv("GOV_DASHBOARD_DECISION_BUFFER", "7")
        assert DecisionLog().capacity == 7
        monkeypatch.setenv("GOV_DASHBOARD_DECISION_BUFFER", "not-a-number")
        assert DecisionLog().capacity == 1000
        monkeypatch.setenv("GOV_DASHBOARD_DECISION_BUFFER", "0")
        assert DecisionLog().capacity == 1000


# ── Decision extraction ──────────────────────────────────────────────────────

class TestDecisionExtraction:
    @pytest.mark.parametrize("status,expected", [
        (200, ALLOW), (204, ALLOW), (400, DENY), (403, DENY), (404, DENY),
        (500, ERROR), (502, ERROR),
    ])
    def test_classify(self, status, expected):
        assert classify(status) == expected

    def test_control_code_from_a_403_body(self):
        body = {"error": "C1", "reason": "tool not on the allow-list"}
        assert control_code(body) == "C1"

    def test_control_code_from_a_symbolic_denial(self):
        assert control_code({"error": "recipient_not_allowed",
                             "reason": "Rogue not permitted"}) == "recipient_not_allowed"

    def test_no_control_code_when_absent_or_not_a_mapping(self):
        assert control_code({"reason": "denied"}) == ""
        assert control_code(None) == ""
        assert control_code("recipient_not_allowed") == ""
        assert control_code({"error": 403}) == ""

    def test_allowed_requests_carry_no_control_code(self):
        log = DecisionLog(capacity=4)
        record = log.record(route="/llm", agent_type="A", nhi_id="n", status=200,
                            payload={"error": "not-a-denial"})
        assert record["control"] == ""


# ── Compiled crosswalk ───────────────────────────────────────────────────────

class TestCrosswalk:
    def test_has_the_full_control_set(self):
        assert len(CROSSWALK) >= 40

    def test_every_row_carries_the_standards_columns(self):
        for row in CROSSWALK:
            assert set(row) == {"code", "name", "module", "owasp", "nist",
                                "iso42001", "eu_ai_act", "atlas"}
            assert row["code"] and row["name"]

    def test_codes_are_unique(self):
        codes = [row["code"] for row in CROSSWALK]
        assert len(set(codes)) == len(codes)

    def test_known_controls_are_present(self):
        indexed = by_code()
        assert indexed["B1"]["name"] == "Prompt-injection guard"
        assert "LLM01" in indexed["B1"]["owasp"]
        assert indexed["I1"]["code"] == "I1"
        assert indexed["N5"]["name"] == "SBOM (SPDX/CycloneDX)"

    def test_generator_check_mode_passes(self):
        result = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "gen_crosswalk.py"), "--check"],
            capture_output=True, text=True, cwd=str(_ROOT),
        )
        assert result.returncode == 0, result.stderr


# ── Rendering ────────────────────────────────────────────────────────────────

def _render(log: DecisionLog, registry: dict | None = None) -> str:
    return render_dashboard(version="1.2.3", revision="abc1234", uptime=61.0,
                            registry=registry or {}, log=log)


class TestRendering:
    def test_zero_state_when_nothing_recorded(self):
        page = _render(DecisionLog(capacity=4))
        assert "No agent requests have reached this service" in page
        assert "No guardrail denials have been recorded" in page

    def test_header_reports_version_revision_and_registry(self):
        page = _render(DecisionLog(capacity=4),
                       registry={"version": "2026.1", "agents": {"FinOps": {}, "Ops": {}}})
        assert "1.2.3" in page and "abc1234" in page
        assert "2 agent types" in page and "2026.1" in page

    def test_a_denial_appears_in_the_run_table_and_the_summary(self):
        log = DecisionLog(capacity=16)
        log.record(route="/a2a", agent_type="FinOps", nhi_id="nhi-finops",
                   status=403, payload={"error": "recipient_not_allowed"})
        page = _render(log)
        assert "recipient_not_allowed" in page
        assert "FinOps" in page and "/a2a" in page
        assert 'class="deny"' in page

    def test_a_named_control_is_labelled_from_the_crosswalk(self):
        log = DecisionLog(capacity=16)
        log.record(route="/llm", agent_type="FinOps", nhi_id="n", status=403,
                   payload={"error": "B1"})
        page = _render(log)
        assert "Prompt-injection guard" in page

    def test_a_denial_identifier_resolves_to_its_crosswalk_control(self):
        log = DecisionLog(capacity=16)
        log.record(route="/a2a", agent_type="FinOps", nhi_id="n", status=403,
                   payload={"error": "recipient_not_allowed"})
        page = _render(log)
        assert "A2A recipient allow-list (I1)" in page

    def test_an_unmappable_denial_identifier_is_shown_unresolved(self):
        log = DecisionLog(capacity=16)
        log.record(route="/llm", agent_type="FinOps", nhi_id="n", status=403,
                   payload={"error": "no_governance_policy"})
        page = _render(log)
        assert "unresolved denial identifier" in page

    def test_crosswalk_section_renders_every_control(self):
        page = _render(DecisionLog(capacity=4))
        for row in CROSSWALK:
            assert row["code"] in page

    def test_page_is_self_contained(self):
        page = _render(DecisionLog(capacity=4))
        assert "http-equiv=\"refresh\"" in page
        assert "<script" not in page
        assert "https://" not in page and "src=" not in page

    def test_metadata_is_escaped(self):
        log = DecisionLog(capacity=4)
        log.record(route="/llm", agent_type="<script>alert(1)</script>",
                   nhi_id="n", status=200)
        page = _render(log)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


# ── Served end to end ────────────────────────────────────────────────────────

@pytest.fixture
def live_service(monkeypatch):
    """The real server on an ephemeral port, with a registry that denies /a2a."""
    registry = {
        "version": "test",
        "default": "deny",
        "agents": {"FinOps": {"allowed_recipients": ["Reporter"]}},
    }
    monkeypatch.setenv("GOV_POLICY_REGISTRY", json.dumps(registry))
    monkeypatch.delenv("GOV_CONTROL_TOKEN", raising=False)

    from galaxy_gov.remote import server as srv
    from galaxy_gov.remote.decision_log import DECISIONS

    DECISIONS.reset()
    srv._handler_cache.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        DECISIONS.reset()


def _get(url: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("content-type", ""), r.read().decode("utf-8")


def test_dashboard_route_serves_html_with_the_crosswalk(live_service):
    status, content_type, page = _get(f"{live_service}/dashboard")
    assert status == 200
    assert content_type.startswith("text/html")
    assert "Governance Dashboard" in page
    assert "B1" in page and "Prompt-injection guard" in page


def test_a_denied_a2a_call_appears_on_the_dashboard(live_service):
    import urllib.error

    req = urllib.request.Request(
        f"{live_service}/a2a", data=json.dumps({"recipient": "Rogue"}).encode("utf-8"),
        headers={"content-type": "application/json", "x-agent-type": "FinOps",
                 "x-nhi-id": "nhi-finops"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403

    _, _, page = _get(f"{live_service}/dashboard")
    assert "recipient_not_allowed" in page
    assert "nhi-finops" in page
    assert 'class="deny"' in page


def test_an_allowed_a2a_call_is_recorded_as_an_allow(live_service):
    req = urllib.request.Request(
        f"{live_service}/a2a", data=json.dumps({"recipient": "Reporter"}).encode("utf-8"),
        headers={"content-type": "application/json", "x-agent-type": "FinOps"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200

    _, _, page = _get(f"{live_service}/dashboard")
    assert 'class="allow"' in page


def test_health_route_is_unchanged(live_service):
    status, content_type, body = _get(f"{live_service}/health")
    assert status == 200 and content_type.startswith("application/json")
    assert json.loads(body)["status"] == "ok"
