"""
LambdaAnalyzer Agent (Phase 1 reference port from agentrepo).

Receives an A2ARequest carrying an AWS Lambda module's source paths, runs a
deterministic complexity score, and asks an LLM to produce a structured
migration analysis (markdown). Read-only — the agent has no file-write tools.

Position in the platform:
  - Tier 2, downstream of Scanner+ASTAnalyzer (today: invoked directly via A2A
    by a runner; future Architect/Coder will consume the analysis.md it emits).
  - Leaf agent — never dispatches outbound A2A.

Mirrors agents.ast_agent's shape:
  - own NHI (LambdaAnalyzer)
  - own MAF client + middleware stack via build_agent()
  - deterministic domain glue (complexity scoring + chunking) runs before
    the LLM; the LLM is asked to interpret, not to count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text
from agents._lib.chunker import chunk_file, needs_chunking
from agents._lib.complexity_scorer import ComplexityResult, score_complexity
from agents.config import load_agent_config_cached
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

# All tunables live in agents/config/lambda_analyzer.yaml.
_config = load_agent_config_cached("lambda-analyzer")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "LambdaAnalysisRequest/v1"
REPORT_SCHEMA = "AnalysisReport/v1"

# Inbound envelope cap. The handler trims source_paths if a caller exceeds
# this; context_paths get the same trim. Aligns with agents/config YAML.
_MAX_FILES_PER_REQUEST = _config.a2a.max_files_per_dispatch or 50
_MAX_FILE_BYTES = _config.max_file_scan_bytes


# ── Request / Response schemas ────────────────────────────────────────────────

@dataclass
class AnalysisReport:
    """Structured body carried in A2AResponse.payload for status=ok replies.

    The full markdown analysis lives in `analysis_markdown`; the typed
    fields above it are produced by the deterministic scorer (not the LLM)
    so downstream consumers (Coder) can branch on level without parsing
    free-form text.
    """
    module: str
    language: str
    complexity_score: int
    complexity_level: str            # LOW | MEDIUM | HIGH | UNKNOWN
    files_included: list[str] = field(default_factory=list)
    files_chunked: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    analysis_markdown: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Source assembly + scoring ─────────────────────────────────────────────────

@dataclass
class _SourceBundle:
    """Internal: result of walking the request payload into a prompt-ready blob."""
    listing: str
    files_included: list[str]
    files_chunked: list[str]
    files_skipped: list[str]


def _collect_source(
    *,
    source_paths: list[str],
    source_dir: Optional[str],
    context_paths: list[str],
) -> _SourceBundle:
    """Build the labelled source listing for the prompt.

    - `source_paths` if provided are authoritative (mirrors agentrepo's flow).
    - Else: rglob `source_dir` for ALL files (filtered to non-hidden).
    - Files larger than `MAX_FILE_BYTES` get chunked; files unreadable get skipped.
    - `context_paths` are appended under a separate header marked read-only.
    """
    parts: list[str] = []
    included: list[str] = []
    chunked: list[str] = []
    skipped: list[str] = []

    primary: list[Path] = []
    if source_paths:
        primary = [Path(p) for p in source_paths[:_MAX_FILES_PER_REQUEST]]
    elif source_dir:
        root = Path(source_dir)
        if root.is_dir():
            primary = sorted(
                p for p in root.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            )[:_MAX_FILES_PER_REQUEST]

    for p in primary:
        if not p.is_file():
            skipped.append(str(p))
            continue
        try:
            if needs_chunking(p):
                for i, ch in enumerate(chunk_file(p)):
                    parts.append(f"--- {p} (chunk {i + 1}/{ch.end_line - ch.start_line + 1} lines) ---\n{ch.content}")
                chunked.append(str(p))
            else:
                content = p.read_text(encoding="utf-8", errors="replace")
                parts.append(f"--- {p} ---\n{content}")
            included.append(str(p))
        except OSError as e:
            logger.warning("lambda_analyzer.read_failed", extra={"path": str(p), "error": str(e)})
            skipped.append(str(p))

    if context_paths:
        parts.append(
            "\n## CONTEXT (read-only — do NOT migrate these files; "
            "treat as an anti-corruption boundary)\n"
        )
        for cp in context_paths[:_MAX_FILES_PER_REQUEST]:
            cpath = Path(cp)
            if cpath.is_file():
                try:
                    content = cpath.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"--- {cpath} (read-only) ---\n{content}")
                    included.append(f"{cpath} (context)")
                except OSError:
                    skipped.append(str(cpath))

    return _SourceBundle(
        listing="\n\n".join(parts),
        files_included=included,
        files_chunked=chunked,
        files_skipped=skipped,
    )


def _aggregate_complexity(files: list[str], language: str) -> ComplexityResult:
    """Sum the deterministic scorer across every primary source file.

    agentrepo's analyzer.py:90 calls score_complexity(source_dir, language) on
    a directory, which the scorer can't read — silently returning UNKNOWN.
    Here we score each file and aggregate, so the breakdown is honest.
    """
    if not files:
        return ComplexityResult(score=0, level="UNKNOWN", details=["No source files to score"])

    total_score = 0
    breakdown: dict[str, int] = {}
    details: list[str] = []
    for f in files:
        result = score_complexity(f, language)
        total_score += result.score
        for k, v in result.breakdown.items():
            breakdown[k] = breakdown.get(k, 0) + v
        if result.details:
            details.append(f"# {f}")
            details.extend(result.details)

    if total_score < 5:
        level = "LOW"
    elif total_score < 15:
        level = "MEDIUM"
    else:
        level = "HIGH"
    return ComplexityResult(score=total_score, level=level, breakdown=breakdown, details=details)


def _build_user_prompt(
    *, module: str, language: str, complexity: ComplexityResult, source_listing: str,
) -> str:
    """Assemble the per-call user prompt. System instructions live in the .md."""
    detail_block = ""
    if complexity.details:
        detail_block = "- Details:\n" + "\n".join(complexity.details) + "\n"
    return (
        f"Analyze the AWS Lambda module '{module}' ({language}) for migration "
        f"to Azure Functions.\n\n"
        f"## Pre-computed Complexity Score (deterministic — do not contradict the counts)\n"
        f"- Overall score: {complexity.score} ({complexity.level})\n"
        f"- Breakdown: {complexity.breakdown}\n"
        f"{detail_block}\n"
        f"## Source Files\n\n{source_listing}\n\n"
        f"Produce the migration analysis document following your system instructions exactly. "
        f"Return ONLY the Markdown document — no surrounding code fence, no commentary."
    )


# ── Agent construction ────────────────────────────────────────────────────────

async def build_lambda_analyzer_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    """LambdaAnalyzer-specific factory wrapper. All wiring lives in build_agent;
    per-agent variations live in agents/config/lambda_analyzer.yaml."""
    return await build_agent("lambda-analyzer", run_id, token_provider=token_provider)


# ── A2A handler ───────────────────────────────────────────────────────────────

class LambdaAnalyzerHandler:
    """Wraps a built LambdaAnalyzer so it can serve A2A requests.

    The handler:
      - validates the request payload matches LambdaAnalysisRequest/v1
      - assembles the source listing (with chunking for big files)
      - runs the deterministic complexity scorer
      - asks the LLM for the analysis.md document
      - returns an A2AResponse carrying an AnalysisReport payload

    Construct once per run; dispatcher.a2a_call invokes `.handle(request)`.
    Optionally writes the markdown to disk if the request payload supplies
    `output_dir` — the runner gets a path back without re-serialising.
    """

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
        source_dir = payload.get("source_dir")
        source_paths = payload.get("source_paths") or []
        context_paths = payload.get("context_paths") or []
        output_dir = payload.get("output_dir")  # optional disk sink

        if not module or not language:
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="invalid_payload",
                    message="LambdaAnalysisRequest/v1 requires module:str and language:str",
                ),
                status=A2AStatus.ERROR,
            )
        if not source_paths and not source_dir:
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="invalid_payload",
                    message="At least one of source_paths or source_dir is required",
                ),
                status=A2AStatus.ERROR,
            )

        # 1. Deterministic source assembly + complexity score (no LLM, no network)
        bundle = _collect_source(
            source_paths=list(source_paths),
            source_dir=source_dir,
            context_paths=list(context_paths),
        )
        complexity = _aggregate_complexity(
            files=[f for f in bundle.files_included if not f.endswith("(context)")],
            language=language,
        )

        # LLM call — MAF AgentTelemetryLayer creates the child span automatically
        user_prompt = _build_user_prompt(
            module=module, language=language,
            complexity=complexity, source_listing=bundle.listing,
        )
        llm_response = await self._agent.run(
            user_prompt,
            options={"extra_headers": {
                "x-galaxy-run-id": request.run_id,
                "x-module-id":     request.module_id,
            }},
        )
        analysis_md = extract_response_text(llm_response).strip()

        # 3. Optional sink: write the markdown for human inspection / next stage
        if output_dir:
            out_path = Path(output_dir) / module / "analysis.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(analysis_md, encoding="utf-8")
            logger.info(
                "lambda_analyzer.analysis_written",
                extra={"path": str(out_path), "bytes": len(analysis_md)},
            )

        report = AnalysisReport(
            module=module,
            language=language,
            complexity_score=complexity.score,
            complexity_level=complexity.level,
            files_included=bundle.files_included,
            files_chunked=bundle.files_chunked,
            files_skipped=bundle.files_skipped,
            analysis_markdown=analysis_md,
        )

        return A2AResponse.ok(
            request=request,
            payload=report.to_dict(),
            payload_schema=REPORT_SCHEMA,
            latency_ms=0.0,    # dispatcher stamps wall-clock latency
        )


