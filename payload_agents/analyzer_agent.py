"""
Generic Analyzer Agent — codebase_type-aware migration analysis.

Supersedes LambdaAnalyzer for non-Lambda repos. Accepts any codebase_type
from governance/mappings/aws-azure-reference.yaml. If codebase_type is not
provided in the request payload, runs RepoClassifier on source_dir to detect
it automatically.

A2A schema:
  request:  AnalysisRequest/v1
  response: AnalysisReport/v1   (same as LambdaAnalyzer — Coder is compatible)

The handler injects mapping context (target services, migration_approach,
key_concerns) into the user prompt so the LLM produces migration analysis
grounded in the canonical AWS→Azure mapping rather than hallucinated advice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import yaml
from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from payload_agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from payload_agents._lib.chunker import chunk_file, needs_chunking
from payload_agents._lib.complexity_scorer import ComplexityResult, score_complexity
from payload_agents._lib.repo_classifier import ClassificationResult, classify_repo
from payload_agents._lib.run_logger import get_run_logger
from payload_agents.config import load_agent_config_cached
from core.interfaces import SecretProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("analyzer")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "AnalysisRequest/v1"
REPORT_SCHEMA = "AnalysisReport/v1"

_MAX_FILES_PER_REQUEST = _config.a2a.max_files_per_dispatch or 60
_MAX_FILE_BYTES = _config.max_file_scan_bytes

_MAPPING_PATH = Path(__file__).resolve().parent.parent / "governance" / "mappings" / "aws-azure-reference.yaml"


# ── Public error type ─────────────────────────────────────────────────────────

class MappingNotFoundError(ValueError):
    """Raised when the classified codebase_type has no entry in the reference mapping."""

    def __init__(self, codebase_type: str, supported: list[str]) -> None:
        self.codebase_type = codebase_type
        self.supported = supported
        super().__init__(
            f"No migration mapping for codebase_type '{codebase_type}'. "
            f"Supported: {supported}. "
            f"Add an entry to governance/mappings/aws-azure-reference.yaml to enable support."
        )


# ── Public schema ─────────────────────────────────────────────────────────────

@dataclass
class AnalysisReport:
    """Structured response payload — same shape as LambdaAnalyzer's report
    so Coder/Tester/Reviewer are compatible without changes."""
    module: str
    language: str
    codebase_type: str
    complexity_score: int
    complexity_level: str             # LOW | MEDIUM | HIGH | UNKNOWN
    target_services: list[str] = field(default_factory=list)
    files_included: list[str] = field(default_factory=list)
    files_chunked: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    analysis_markdown: str = ""
    classifier_confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Mapping loader ────────────────────────────────────────────────────────────

def _load_mapping() -> dict:
    """Load and cache the reference mapping YAML."""
    return yaml.safe_load(_MAPPING_PATH.read_text(encoding="utf-8"))


def _find_repo_type(mapping: dict, codebase_type: str) -> Optional[dict]:
    for rt in mapping.get("repository_types", []):
        if rt.get("codebase_type") == codebase_type:
            return rt
    return None


def _supported_types(mapping: dict) -> list[str]:
    return [rt["codebase_type"] for rt in mapping.get("repository_types", [])]


# ── Source assembly ───────────────────────────────────────────────────────────

@dataclass
class _SourceBundle:
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
                    parts.append(f"--- {p} (chunk {i + 1}) ---\n{ch.content}")
                chunked.append(str(p))
            else:
                parts.append(f"--- {p} ---\n{p.read_text(encoding='utf-8', errors='replace')}")
            included.append(str(p))
        except OSError as exc:
            logger.warning("analyzer.read_failed", extra={"path": str(p), "error": str(exc)})
            skipped.append(str(p))

    if context_paths:
        parts.append(
            "\n## CONTEXT (read-only — anti-corruption boundary; do NOT migrate these)\n"
        )
        for cp in context_paths[:_MAX_FILES_PER_REQUEST]:
            cpath = Path(cp)
            if cpath.is_file():
                try:
                    parts.append(f"--- {cpath} (read-only) ---\n{cpath.read_text(encoding='utf-8', errors='replace')}")
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
    if not files:
        return ComplexityResult(score=0, level="UNKNOWN", details=["No source files to score"])
    total = 0
    breakdown: dict[str, int] = {}
    details: list[str] = []
    for f in files:
        r = score_complexity(f, language)
        total += r.score
        for k, v in r.breakdown.items():
            breakdown[k] = breakdown.get(k, 0) + v
        if r.details:
            details.append(f"# {f}")
            details.extend(r.details)
    level = "LOW" if total < 5 else "MEDIUM" if total < 15 else "HIGH"
    return ComplexityResult(score=total, level=level, breakdown=breakdown, details=details)


# ── Prompt assembly ───────────────────────────────────────────────────────────

def _build_user_prompt(
    *,
    module: str,
    language: str,
    codebase_type: str,
    repo_type: dict,
    complexity: ComplexityResult,
    source_listing: str,
) -> str:
    target_services = repo_type.get("target_services", [])
    migration_steps = repo_type.get("migration_approach", [])
    key_concerns = repo_type.get("key_concerns", [])
    source_runtime = repo_type.get("source_runtime", "")
    target_runtime = repo_type.get("target_runtime", "")

    mapping_block = (
        f"## Canonical Migration Mapping (from governance/mappings/aws-azure-reference.yaml)\n"
        f"- codebase_type: `{codebase_type}`\n"
        f"- Source runtime: {source_runtime}\n"
        f"- Target runtime: {target_runtime}\n"
        f"- Target Azure services: {', '.join(target_services)}\n\n"
        f"### Standard migration steps for this type\n"
        + "\n".join(f"{i+1}. {step}" for i, step in enumerate(migration_steps))
        + "\n\n### Key concerns / gotchas\n"
        + "\n".join(f"- {c}" for c in key_concerns)
        + "\n"
    )

    detail_block = ""
    if complexity.details:
        detail_block = "- Details:\n" + "\n".join(complexity.details[:20]) + "\n"

    return (
        f"Analyze module '{module}' ({language}, type={codebase_type}) for migration.\n\n"
        f"## Pre-computed Complexity Score (deterministic — do not contradict the counts)\n"
        f"- Score: {complexity.score} ({complexity.level})\n"
        f"- Breakdown: {complexity.breakdown}\n"
        f"{detail_block}\n"
        f"{mapping_block}\n"
        f"## Source Files\n\n{source_listing}\n\n"
        f"Produce the migration analysis document following your system instructions. "
        f"Ground every AWS dependency finding in the canonical mapping above. "
        f"Return ONLY the Markdown document — no code fence, no commentary."
    )


# ── Agent construction ────────────────────────────────────────────────────────

async def build_analyzer_agent(
    run_id: str,
    token_provider: Optional[SecretProvider] = None,
) -> AgentBundle:
    return await build_agent("analyzer", run_id, token_provider=token_provider)


# ── A2A handler ───────────────────────────────────────────────────────────────

class AnalyzerHandler:
    """A2A handler for AnalysisRequest/v1 → AnalysisReport/v1.

    Accepts any codebase_type from the reference mapping. If codebase_type is
    absent from the payload, classifies source_dir automatically. Raises
    MappingNotFoundError (which the orchestrator catches) when the type has no
    entry in aws-azure-reference.yaml.
    """

    def __init__(self, agent: Agent, *, nhi_id: str = "") -> None:
        self._agent = agent
        self._nhi_id = nhi_id
        self._mapping = _load_mapping()

    async def handle(self, request: A2ARequest) -> A2AResponse:
        if request.payload_schema != REQUEST_SCHEMA:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="schema_mismatch",
                               message=f"Expected {REQUEST_SCHEMA}, got {request.payload_schema}"),
                status=A2AStatus.ERROR,
            )

        payload = request.payload or {}
        module = (payload.get("module") or "").strip()
        language = (payload.get("language") or "python").strip()
        source_dir = payload.get("source_dir")
        source_paths = list(payload.get("source_paths") or [])
        context_paths = list(payload.get("context_paths") or [])
        output_dir = payload.get("output_dir")
        codebase_type = (payload.get("codebase_type") or "").strip()
        classifier_confidence = 0.0

        if not module:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="AnalysisRequest/v1 requires module:str"),
                status=A2AStatus.ERROR,
            )
        if not source_paths and not source_dir:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="At least one of source_paths or source_dir is required"),
                status=A2AStatus.ERROR,
            )

        # Auto-classify if codebase_type not provided
        if not codebase_type and source_dir:
            result: ClassificationResult = classify_repo(source_dir)
            if result.codebase_type is None:
                supported = _supported_types(self._mapping)
                return A2AResponse.error(
                    request=request,
                    error=A2AError(
                        code="mapping_not_found",
                        message=(
                            f"RepoClassifier could not identify a supported codebase_type "
                            f"in '{source_dir}'. Supported types: {supported}. "
                            f"Classifier scores: {result.scores}"
                        ),
                    ),
                    status=A2AStatus.ERROR,
                )
            codebase_type = result.codebase_type
            classifier_confidence = result.confidence
            logger.info(
                "analyzer.classified",
                extra={"codebase_type": codebase_type, "confidence": classifier_confidence},
            )

        # Look up mapping entry
        repo_type = _find_repo_type(self._mapping, codebase_type)
        if repo_type is None:
            supported = _supported_types(self._mapping)
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="mapping_not_found",
                    message=(
                        f"No mapping for codebase_type '{codebase_type}'. "
                        f"Supported: {supported}"
                    ),
                ),
                status=A2AStatus.ERROR,
            )

        bundle = _collect_source(
            source_paths=source_paths, source_dir=source_dir, context_paths=context_paths,
        )
        complexity = _aggregate_complexity(
            files=[f for f in bundle.files_included if not f.endswith("(context)")],
            language=language,
        )

        # LLM call — MAF AgentTelemetryLayer creates the child span automatically
        t0 = time.perf_counter()
        user_prompt = _build_user_prompt(
            module=module, language=language,
            codebase_type=codebase_type, repo_type=repo_type,
            complexity=complexity, source_listing=bundle.listing,
        )
        llm_response = await self._agent.run(
            user_prompt,
            options={"extra_headers": {
                "x-galaxy-run-id": request.run_id,
                "x-module-id": request.module_id,
            }},
        )
        analysis_md = extract_response_text(llm_response).strip()
        tokens_in, tokens_out = extract_usage(llm_response)

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=1, module=module,
                codebase_type=codebase_type, latency_ms=latency_ms,
                tokens_in=tokens_in, tokens_out=tokens_out,
            )

        if output_dir:
            out_path = Path(output_dir) / module / "analysis.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(analysis_md, encoding="utf-8")

        report = AnalysisReport(
            module=module,
            language=language,
            codebase_type=codebase_type,
            complexity_score=complexity.score,
            complexity_level=complexity.level,
            target_services=repo_type.get("target_services", []),
            files_included=bundle.files_included,
            files_chunked=bundle.files_chunked,
            files_skipped=bundle.files_skipped,
            analysis_markdown=analysis_md,
            classifier_confidence=classifier_confidence,
        )

        return A2AResponse.ok(
            request=request,
            payload=report.to_dict(),
            payload_schema=REPORT_SCHEMA,
            latency_ms=latency_ms,
        )


