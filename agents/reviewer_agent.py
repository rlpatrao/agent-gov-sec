"""
Reviewer Agent (Phase 2A port from agentrepo).

8-point quality-gate review on a migrated Azure Functions module. Receives
analysis + tests + sprint contract + migrated source via A2A; returns a
typed ReviewReport with recommendation + confidence + blocking issues, plus
the full review markdown.

Read-only — no @tool wiring. The host (this module's domain code) inlines
every input file into the prompt; the LLM does interpretive work only.
This matches what agentrepo's prompt itself instructs:
  "Your output is a markdown body. Do NOT use write_file to emit the review."

Mirrors agents.lambda_analyzer_agent's shape:
  - own NHI (Reviewer)
  - own MAF client + middleware stack via build_agent()
  - deterministic content collection runs before the LLM
  - structured response parsing happens after, on the markdown body
"""

from __future__ import annotations

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
from agents.config import load_agent_config_cached
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("reviewer")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "ReviewRequest/v1"
REPORT_SCHEMA = "ReviewReport/v1"

# Cap on inlined source files. Reviewer prompts already get analysis +
# tests + contract; we don't want to blow the context budget on a runaway
# source tree.
_MAX_SOURCE_FILES = 80


# ── Public schema ─────────────────────────────────────────────────────────────

@dataclass
class ReviewReport:
    """Structured body for A2AResponse.payload on status=ok."""
    module: str
    recommendation: str             # APPROVE | CHANGES_REQUESTED | BLOCKED
    confidence: int                 # 0-100; 0 if not parseable
    blocking_issues: list[str] = field(default_factory=list)
    files_reviewed: list[str] = field(default_factory=list)
    files_chunked: list[str] = field(default_factory=list)
    review_markdown: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Content assembly ──────────────────────────────────────────────────────────

@dataclass
class _ReviewBundle:
    sections: list[str]
    files_reviewed: list[str]
    files_chunked: list[str]


def _collect_review_inputs(
    *,
    analysis_markdown: Optional[str],
    sprint_contract_json: Optional[str],
    test_results_markdown: Optional[str],
    failure_reports_json: Optional[str],
    migrated_source_dir: str,
    infra_dir: Optional[str],
) -> _ReviewBundle:
    """Build the labelled context-section list the prompt will inline.

    Sections are concatenated with blank lines in the order the Reviewer
    prompt expects: analysis → contract → test results → failures → source → infra.
    Caller passes raw strings for stage artifacts (analysis, contract, etc.) so
    this agent doesn't have to re-read disk for upstream stages.
    """
    sections: list[str] = []
    if analysis_markdown:
        sections.append(f"## Analyzer Output\n{analysis_markdown}")
    if sprint_contract_json:
        sections.append(f"## Sprint Contract\n```json\n{sprint_contract_json}\n```")
    if test_results_markdown:
        sections.append(f"## Test Results\n{test_results_markdown}")
    if failure_reports_json:
        sections.append(f"## Failure Reports\n```json\n{failure_reports_json}\n```")

    reviewed: list[str] = []
    chunked: list[str] = []

    src_root = Path(migrated_source_dir)
    if src_root.is_dir():
        source_parts: list[str] = []
        files = sorted(
            p for p in src_root.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        )[:_MAX_SOURCE_FILES]
        for fpath in files:
            try:
                if needs_chunking(fpath):
                    chunks = chunk_file(fpath)
                    for i, ch in enumerate(chunks):
                        source_parts.append(
                            f"--- {fpath} (chunk {i + 1}/{len(chunks)}) ---\n{ch.content}"
                        )
                    chunked.append(str(fpath))
                else:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    source_parts.append(f"--- {fpath} ---\n{content}")
                reviewed.append(str(fpath))
            except OSError as e:
                logger.warning(
                    "reviewer.read_failed",
                    extra={"path": str(fpath), "error": str(e)},
                )
        if source_parts:
            sections.append("## Migrated Source Code\n" + "\n\n".join(source_parts))

    if infra_dir:
        infra_root = Path(infra_dir)
        if infra_root.is_dir():
            for fpath in sorted(infra_root.rglob("*")):
                if fpath.is_file():
                    try:
                        content = fpath.read_text(encoding="utf-8", errors="replace")
                        sections.append(
                            f"## Infrastructure: {fpath.name}\n```\n{content}\n```"
                        )
                        reviewed.append(str(fpath))
                    except OSError:
                        continue

    return _ReviewBundle(sections=sections, files_reviewed=reviewed, files_chunked=chunked)


def _build_user_prompt(*, module: str, language: str, bundle: _ReviewBundle) -> str:
    """Per-call user prompt. System checklist lives in the .md."""
    return (
        f"Review the migrated Azure Function module '{module}' ({language}).\n\n"
        + "\n\n".join(bundle.sections)
        + "\n\n"
        + "Perform the 8-point quality gate review and return ONLY the markdown body "
        + "described in your system instructions — no surrounding code fence, no commentary."
    )


# ── Output parsing ────────────────────────────────────────────────────────────

_RECOMMENDATION_LABELED = re.compile(
    r"RECOMMENDATION[^A-Z]{0,40}(APPROVE|BLOCKED|CHANGES[_ ]REQUESTED)",
    re.IGNORECASE,
)
_CONFIDENCE = re.compile(r"Confidence Score:\s*(\d+)\s*/\s*100", re.IGNORECASE)
# Anchor to start-of-line so "### Non-Blocking" doesn't match "### Blocking";
# use [^\S\n]* (any whitespace except newline) so trailing spaces are tolerated
# without the regex eating the blank line that follows the heading and bleeding
# into the next section.
_BLOCKING_SECTION = re.compile(
    r"^###[^\S\n]*Blocking[^\S\n]*\n(.*?)(?=^###|^##[^\S\n]|\Z)",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)


def parse_review_output(raw: str) -> tuple[str, int, list[str]]:
    """Return (recommendation, confidence, blocking_issues) parsed from the markdown.

    Defaults are conservative: CHANGES_REQUESTED, 0, []. The recommendation
    parser is tolerant — labelled verdict wins, otherwise scan the tail for
    APPROVE/BLOCKED tokens.
    """
    norm = raw.upper()
    recommendation = "CHANGES_REQUESTED"
    labeled = _RECOMMENDATION_LABELED.search(norm)
    if labeled:
        verdict = labeled.group(1).replace(" ", "_")
        if verdict == "APPROVE":
            recommendation = "APPROVE"
        elif verdict == "BLOCKED":
            recommendation = "BLOCKED"
        else:
            recommendation = "CHANGES_REQUESTED"
    else:
        tail = norm[-400:]
        if re.search(r"\bAPPROVE\b", tail) and "NOT APPROVE" not in tail:
            recommendation = "APPROVE"
        elif re.search(r"\bBLOCKED\b", tail):
            recommendation = "BLOCKED"

    confidence = 0
    cm = _CONFIDENCE.search(raw)
    if cm:
        try:
            confidence = max(0, min(100, int(cm.group(1))))
        except ValueError:
            confidence = 0

    blocking: list[str] = []
    bm = _BLOCKING_SECTION.search(raw)
    if bm:
        for line in bm.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- "):
                blocking.append(line[2:].strip())

    return recommendation, confidence, blocking


# ── Agent construction ────────────────────────────────────────────────────────

async def build_reviewer_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    """Reviewer-specific factory wrapper. All wiring lives in build_agent."""
    return await build_agent("reviewer", run_id, token_provider=token_provider)


# ── A2A handler ───────────────────────────────────────────────────────────────

class ReviewerHandler:
    """A2A handler for ReviewRequest/v1 → ReviewReport/v1."""

    def __init__(self, agent: Agent, nhi_id: str = "") -> None:
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
        migrated_source_dir = payload.get("migrated_source_dir")
        if not module or not language or not migrated_source_dir:
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="invalid_payload",
                    message="ReviewRequest/v1 requires module, language, migrated_source_dir",
                ),
                status=A2AStatus.ERROR,
            )

        # 1. Deterministic content assembly (no LLM)
        bundle = _collect_review_inputs(
            analysis_markdown=payload.get("analysis_markdown"),
            sprint_contract_json=payload.get("sprint_contract_json"),
            test_results_markdown=payload.get("test_results_markdown"),
            failure_reports_json=payload.get("failure_reports_json"),
            migrated_source_dir=migrated_source_dir,
            infra_dir=payload.get("infra_dir"),
        )

        # 2. LLM call — MAF AgentTelemetryLayer creates the child span automatically
        t0 = time.perf_counter()
        user_prompt = _build_user_prompt(module=module, language=language, bundle=bundle)
        llm_response = await self._agent.run(
            user_prompt,
            options={"extra_headers": {
                "x-galaxy-run-id": request.run_id,
                "x-module-id":     request.module_id,
            }},
        )
        review_md = extract_response_text(llm_response).strip()
        tokens_in, tokens_out = extract_usage(llm_response)

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=1, module=module,
                latency_ms=latency_ms,
                tokens_in=tokens_in, tokens_out=tokens_out,
            )

        recommendation, confidence, blocking = parse_review_output(review_md)

        # 3. Optional disk sink
        output_dir = payload.get("output_dir")
        if output_dir:
            out_path = Path(output_dir) / module / "review.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(review_md, encoding="utf-8")
            logger.info(
                "reviewer.review_written",
                extra={"path": str(out_path), "bytes": len(review_md)},
            )

        report = ReviewReport(
            module=module,
            recommendation=recommendation,
            confidence=confidence,
            blocking_issues=blocking,
            files_reviewed=bundle.files_reviewed,
            files_chunked=bundle.files_chunked,
            review_markdown=review_md,
        )

        return A2AResponse.ok(
            request=request,
            payload=report.to_dict(),
            payload_schema=REPORT_SCHEMA,
            latency_ms=latency_ms,
        )


