"""
tests/test_report_html.py — the HTML report renders with the unified matrix and
the per-control what/why catalogue.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "report_html", Path(__file__).resolve().parent.parent / "scripts" / "report_html.py")
report_html = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = report_html
_SPEC.loader.exec_module(report_html)  # type: ignore[union-attr]


def _check(feature, agent, scenario, ok, actual="", model_dep=False):
    return SimpleNamespace(feature=feature, agent=agent, scenario=scenario, ok=ok,
                           actual=actual, model_dep=model_dep, expected="")


def _row(code, guard, mode, scenario, ok, intercepted):
    return SimpleNamespace(code=code, guard=guard, mode=mode, scenario=scenario,
                           ok=ok, intercepted=intercepted, outcome="x")


def test_renders_unified_report(tmp_path):
    out = tmp_path / "report.html"
    baseline = [
        _check("A1 NHI identity", "FinOps", "resolve principal", True),
        _check("B4 prompt-injection", "Rogue", "block override", True, actual="blocked"),
    ]
    extended = [
        _row("EG01", "egress-policy", "WIRED", "evil url", True, True),
        _row("EG01", "egress-policy", "WIRED", "allowlisted url", True, False),
        _row("AD28", "adversarial-redteam", "OPS", "defense rate", True, True),
    ]
    path = report_html.render_report(
        str(out), generated="2026-06-14 10:00", cloud="local", framework="raw",
        mode="deterministic", baseline_checks=baseline,
        baseline_control_map={"A1": "NHI identity", "B4": "Prompt-injection guard"},
        extended_rows=extended, real=False,
    )
    assert Path(path).exists()
    h = out.read_text(encoding="utf-8")
    # structure
    assert "<!doctype html>" in h and "</html>" in h
    # both matrices, each self-contained with description / input / output columns
    assert "Baseline matrix" in h and "Extended sweep" in h
    assert "<th>Description</th>" in h and "<th>Input</th>" in h and "<th>Output</th>" in h
    # codes present
    assert "A1" in h and "EG01" in h and "AD28" in h
    # flag/hook + the inline control description (no other doc needed to read the row)
    assert "GALAXY_GAP_EGRESS_POLICY" in h
    assert "Outbound URL allow-list" in h            # EG01 description, inline
    assert "Per-agent Non-Human Identity" in h       # A1 baseline description, inline
    # the input + output values appear in the row
    assert "evil url" in h                            # EG01 input (scenario)
    # tallies: 2 baseline + 3 extended = 5 checks, distinct controls A1/B4 + EG01/AD28 = 4
    assert "5/5" in h
    assert "4 controls" in h


def test_creates_missing_parent_dir(tmp_path):
    """--html docs/output/report.html must work even when the dir does not exist."""
    out = tmp_path / "nested" / "dir" / "report.html"
    assert not out.parent.exists()
    path = report_html.render_report(
        str(out), generated="t", cloud="local", framework="raw", mode="deterministic",
        baseline_checks=[_check("A1 NHI identity", "FinOps", "x", True)],
        baseline_control_map={"A1": "NHI identity"}, extended_rows=[], real=False)
    assert Path(path).exists()
    assert "<!doctype html>" in Path(path).read_text(encoding="utf-8")


def test_real_mode_marks_na(tmp_path):
    out = tmp_path / "r.html"
    baseline = [_check("B7 capability", "Rogue", "adversarial tool", False, model_dep=True)]
    report_html.render_report(
        str(out), generated="t", cloud="azure", framework="langgraph", mode="real",
        baseline_checks=baseline, baseline_control_map={"B7": "Capability guard"},
        extended_rows=[], real=True)
    h = out.read_text(encoding="utf-8")
    assert "N/A" in h  # model-dependent miss in real mode is N/A, not FAIL
