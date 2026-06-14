"""
tests/test_extended_guardrails.py — assert the full-sweep guardrail walk.

Drives ``scripts/demo_extended_guardrails.py`` end to end and asserts every
control's pass case and intercept case behave as expected. This is the
regression gate for the sweep: it exercises each guard *through the live
GuardPipeline / governed agent invocation* (not just the wrapper), so a change
that silently un-wires a guard fails here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "demo_extended_guardrails",
    Path(__file__).resolve().parent.parent / "scripts" / "demo_extended_guardrails.py",
)
demo = importlib.util.module_from_spec(_SPEC)
# Register before exec so the module's @dataclass can resolve its own module.
sys.modules[_SPEC.name] = demo
_SPEC.loader.exec_module(demo)  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def walk():
    """Run the full conformance walk once; return the recorded rows."""
    demo.RESULTS.clear()
    rc = asyncio.run(demo.main())
    return rc, list(demo.RESULTS)


def test_all_checks_pass(walk):
    rc, rows = walk
    failed = [f"{r.code} {r.guard} [{r.mode}] {r.scenario} → {r.outcome}" for r in rows if not r.ok]
    assert not failed, "guardrail checks failed:\n" + "\n".join(failed)
    assert rc == 0


def test_covers_all_controls(walk):
    _, rows = walk
    codes = {r.code for r in rows}
    # 28 controls: 10 wired + 5 registered + 5 direct + 8 operational.
    assert len(codes) == 28, f"expected 28 controls, got {len(codes)}: {sorted(codes)}"


def test_every_control_has_pass_and_intercept(walk):
    """Each control must demonstrate at least one intercept and one pass-through,
    so no guard is a no-op or an always-block."""
    _, rows = walk
    by_code: dict[str, list] = {}
    for r in rows:
        by_code.setdefault(r.code, []).append(r)
    # Intercept-only controls: circuit-breaker open state, plus the operational
    # capabilities that assert the compliant-vs-breach outcome in a single combined
    # row (SLO burn, accuracy breach, eval fail, replay regression, tamper detect,
    # withheld certification, adversarial defense rate).
    intercept_only = {"CB02", "SLO21", "AC22", "EV23", "RP24", "SG26", "CT27", "AD28"}
    # controls with no intercept case (a generation/reporting capability, not a gate)
    pass_only = {"SB25"}  # SBOM generation — produces an artifact, nothing to block
    for code, rs in by_code.items():
        has_intercept = any(r.intercepted and r.ok for r in rs)
        has_pass = any((not r.intercepted) and r.ok for r in rs)
        if code not in intercept_only:
            assert has_pass, f"{code}: no pass-through case"
        if code not in pass_only:
            assert has_intercept, f"{code}: no intercept case"


def test_interceptions_demonstrated(walk):
    _, rows = walk
    intercepts = sum(1 for r in rows if r.intercepted and r.ok)
    assert intercepts >= 25, f"expected >=25 demonstrated interceptions, got {intercepts}"
