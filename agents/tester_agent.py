"""
Tester Agent (Phase 2B port from agentrepo).

Three-layer evaluation: pytest pass/fail (Layer 1) + LLM SDK-mock audit
(Layer 2) + LLM contract verification (Layer 3). Tools: a sandboxed
pytest runner. Returns a typed TestReport with verdict + structured
failures the Coder consumes on retry.

Sandbox model
-------------
Built once per run with a single `sandbox_root` Path. The run_tests tool
refuses any test_dir outside that root. Subprocess runs with cwd locked
to test_dir, env scrubbed of Azure/APIM secrets, hard 120s timeout.
CapabilityGuard enforces the tool name allow-list as a second line of defence.

Self-healing (multi-attempt) is owned by the orchestrator. The A2A payload
carries `attempt`; the handler stamps `galaxy.attempt=N` on the OTel span.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.chunker import chunk_file, needs_chunking
from agents._lib.run_logger import get_run_logger
from agents._lib.test_runner import make_run_tests
from agents.config import load_agent_config_cached
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("tester")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "TestRequest/v1"
REPORT_SCHEMA = "TestReport/v1"

_MAX_SOURCE_FILES = 60


# ── Public schema ─────────────────────────────────────────────────────────────

@dataclass
class TestFailure:
    __test__ = False  # not a pytest test class
    """Structured failure entry for the Coder's self-healing loop.

    Mirrors agentrepo's eval-failures.json schema: failure_id, layer,
    error_category, description, file/line/expected/actual context,
    self_healing_strategy.
    """
    failure_id: str
    layer: str
    error_category: str
    description: str
    file: str = ""
    line: int = 0
    expected: str = ""
    actual: str = ""
    self_healing_strategy: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TestReport:
    __test__ = False  # not a pytest test class
    """Structured body for A2AResponse.payload on status=ok."""
    module: str
    attempt: int
    verdict: str                     # PASS | FAIL | PARTIAL | UNKNOWN
    failures: list[dict] = field(default_factory=list)
    test_results_markdown: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Source assembly + prompt build ────────────────────────────────────────────

def _collect_inputs(*, migrated_source_dir: Path) -> str:
    """Inline the migrated source so the LLM can audit Layer 2 (mock shape)
    without filesystem tools. Mirrors Reviewer's _collect_review_inputs."""
    if not migrated_source_dir.is_dir():
        return ""
    parts: list[str] = []
    files = sorted(
        p for p in migrated_source_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )[:_MAX_SOURCE_FILES]
    for p in files:
        try:
            if needs_chunking(p):
                for i, ch in enumerate(chunk_file(p)):
                    parts.append(f"--- {p} (chunk {i + 1}) ---\n{ch.content}")
            else:
                parts.append(f"--- {p} ---\n{p.read_text(encoding='utf-8', errors='replace')}")
        except OSError:
            continue
    return "\n\n".join(parts)


def _build_user_prompt(
    *, module: str, language: str, attempt: int,
    test_dir: str, source_listing: str,
    sprint_contract_json: Optional[str],
    previous_failures_json: Optional[str],
) -> str:
    """Per-call user prompt for the Tester."""
    sections: list[str] = []
    if sprint_contract_json:
        sections.append(f"## Sprint Contract\n```json\n{sprint_contract_json}\n```")
    sections.append(f"## Migrated Source Code\n{source_listing}")
    if previous_failures_json:
        sections.append(
            "## Previous Failure Report\n"
            f"```json\n{previous_failures_json}\n```\n"
            "Do NOT repeat the same checks; cover what the prior attempt missed."
        )

    return (
        f"Evaluate the migrated Azure Function for module '{module}' ({language}).\n"
        f"This is attempt {attempt}/3.\n\n"
        f"Run `run_tests(\"{test_dir}\")` to execute the unit suite, then perform "
        f"Layer 2 (SDK mock-shape) and Layer 3 (contract) analysis on the inlined "
        f"migrated source below.\n\n"
        + "\n\n".join(sections)
        + "\n\n"
        + "Return ONLY the markdown body specified in your system instructions — "
        + "no surrounding code fence, no commentary."
    )


# ── Output parsing ───────────────────────────────────────────────────────────

_VERDICT_LINE = re.compile(
    r"Overall\s+Verdict[^A-Z]{0,20}(PASS|FAIL|PARTIAL)",
    re.IGNORECASE,
)


def parse_test_output(raw: str) -> tuple[str, list[dict]]:
    """Return (verdict, structured_failures) parsed from the markdown.

    Verdict defaults to UNKNOWN when no labelled line is found. Failures
    are picked up from JSON blocks (one object per line) under the
    `## Structured Failures` heading — tolerates either JSON code fences
    or bare JSON objects.
    """
    verdict = "UNKNOWN"
    m = _VERDICT_LINE.search(raw)
    if m:
        verdict = m.group(1).upper()
    elif re.search(r"\bFAIL\b", raw[-400:].upper()):
        verdict = "FAIL"
    elif re.search(r"\bPASS\b", raw[-400:].upper()):
        verdict = "PASS"

    failures: list[dict] = []
    # Find the Structured Failures section (case insensitive). Capture
    # everything until the next ## heading or end of document.
    sec = re.search(
        r"^##\s*Structured\s+Failures\s*\n(.*?)(?=^##\s|\Z)",
        raw, re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    if sec:
        body = sec.group(1)
        # Strip ```json fences if present, then parse each non-empty line as JSON.
        body = re.sub(r"```(?:json)?\s*", "", body).replace("```", "")
        for line in body.split("\n"):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                failures.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("tester.failure_line_unparseable", extra={"line": line[:200]})

    return verdict, failures


# ── Agent construction ────────────────────────────────────────────────────────

async def build_tester_agent(
    run_id: str,
    *,
    sandbox_root: str | Path,
    timeout_seconds: int = 120,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    """Build the Tester with a sandbox-bound run_tests tool.

    `sandbox_root` is the only place run_tests may exec against. Matches
    Coder's sandbox: typically the orchestrator passes MIGRATED_DIR for both.
    `timeout_seconds` is the pytest hard timeout (kill -9 after this).
    """
    run_tests_tool = make_run_tests(sandbox_root, timeout_seconds=timeout_seconds)
    return await build_agent(
        "tester", run_id,
        token_provider=token_provider,
        tools=[run_tests_tool],
    )


# ── A2A handler ──────────────────────────────────────────────────────────────

class TesterHandler:
    """A2A handler for TestRequest/v1 → TestReport/v1."""

    # Suppress pytest's "Test*" class auto-collection — this is a handler,
    # not a test class. Without this, pytest emits a collection warning.
    __test__ = False

    def __init__(self, agent: Agent, *, nhi_id: str = "") -> None:
        self._agent = agent
        self._nhi_id = nhi_id

    async def handle(self, request: A2ARequest) -> A2AResponse:
        if request.payload_schema != REQUEST_SCHEMA:
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="schema_mismatch",
                    message=f"Expected {REQUEST_SCHEMA}, got {request.payload_schema}",
                ),
                status=A2AStatus.ERROR,
            )

        payload = request.payload or {}
        module = (payload.get("module") or "").strip()
        language = (payload.get("language") or "").strip()
        attempt = int(payload.get("attempt") or 1)
        migrated_source_dir = payload.get("migrated_source_dir")
        test_dir = payload.get("test_dir")
        if not module or not language or not migrated_source_dir or not test_dir:
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="invalid_payload",
                    message="TestRequest/v1 requires module, language, migrated_source_dir, test_dir",
                ),
                status=A2AStatus.ERROR,
            )

        src_path = Path(migrated_source_dir)
        source_listing = _collect_inputs(migrated_source_dir=src_path)

        # LLM call — MAF AgentTelemetryLayer creates the child span automatically
        t0 = time.perf_counter()
        user_prompt = _build_user_prompt(
            module=module, language=language, attempt=attempt,
            test_dir=test_dir, source_listing=source_listing,
            sprint_contract_json=payload.get("sprint_contract_json"),
            previous_failures_json=payload.get("previous_failures_json"),
        )
        llm_response = await self._agent.run(
            user_prompt,
            options={"extra_headers": {
                "x-galaxy-run-id": request.run_id,
                "x-module-id":     request.module_id,
            }},
        )
        results_md = extract_response_text(llm_response).strip()
        tokens_in, tokens_out = extract_usage(llm_response)

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=attempt, module=module,
                latency_ms=latency_ms,
                tokens_in=tokens_in, tokens_out=tokens_out,
            )

        verdict, failures = parse_test_output(results_md)

        # Optional disk sink: write test-results.md + eval-failures.json
        # (matches agentrepo's filenames so an existing pipeline can consume).
        output_dir = payload.get("output_dir")
        if output_dir:
            out_root = Path(output_dir) / module
            out_root.mkdir(parents=True, exist_ok=True)
            (out_root / "test-results.md").write_text(results_md, encoding="utf-8")
            if failures:
                eval_failures = {
                    "module": module,
                    "attempt": attempt,
                    "overall_verdict": verdict,
                    "failures": failures,
                    "prior_attempts": [],
                }
                (out_root / "eval-failures.json").write_text(
                    json.dumps(eval_failures, indent=2), encoding="utf-8",
                )

        report = TestReport(
            module=module,
            attempt=attempt,
            verdict=verdict,
            failures=failures,
            test_results_markdown=results_md,
        )

        return A2AResponse.ok(
            request=request,
            payload=report.to_dict(),
            payload_schema=REPORT_SCHEMA,
            latency_ms=latency_ms,
        )


