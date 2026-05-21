"""
SecurityReviewer Agent (Phase 2A port from agentrepo).

OWASP-based security review on a migrated Azure Functions module. Two-phase:
  1. Domain code runs `scan_directory()` deterministically over the migrated
     source — produces hard findings (BLOCK/WARN/INFO) the LLM must address.
  2. LLM does the deep analysis (logic vulns, IDOR, auth bypass, Azure-specific
     misuse) using the regex findings as a starting checklist.

Read-only — no @tool wiring. The deterministic scan is authoritative for the
patterns it covers; the LLM's verdict can NEVER downgrade an automated BLOCK
to APPROVE (enforced after the LLM call by `_combine_recommendation`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.run_logger import get_run_logger
from agents._lib.security_scanner import SecurityFinding, scan_directory
from agents.config import load_agent_config_cached
from agents.reviewer_agent import parse_review_output  # share recommendation parser
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("security-reviewer")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "SecurityReviewRequest/v1"
REPORT_SCHEMA = "SecurityReviewReport/v1"


# ── Public schema ─────────────────────────────────────────────────────────────

@dataclass
class SecurityReviewReport:
    """Structured body for A2AResponse.payload on status=ok."""
    module: str
    recommendation: str             # APPROVE | CHANGES_REQUESTED | BLOCKED
    confidence: int                 # 0-100; from LLM's narrative if present
    block_count: int
    warn_count: int
    info_count: int
    automated_findings: list[dict] = field(default_factory=list)
    review_markdown: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Deterministic scan + prompt assembly ──────────────────────────────────────

def _format_findings_table(findings: list[SecurityFinding]) -> str:
    """Render findings as a markdown table to inline in the prompt."""
    if not findings:
        return "No automated findings."
    lines = [
        "| File | Line | Category | Severity | Pattern Matched |",
        "|---|---|---|---|---|",
    ]
    for f in findings:
        # Pipes in matched lines would break the table — escape them.
        snippet = f.description.replace("|", r"\|")
        path = f.file.replace("|", r"\|")
        lines.append(f"| {path} | {f.line} | {f.category} | {f.severity} | {snippet} |")
    return "\n".join(lines)


def _build_user_prompt(
    *, module: str, language: str, source_dir: str,
    findings: list[SecurityFinding], source_listing: str,
) -> str:
    """Per-call user prompt. System checklist lives in the .md."""
    block = sum(1 for f in findings if f.severity == "BLOCK")
    warn = sum(1 for f in findings if f.severity == "WARN")
    info = sum(1 for f in findings if f.severity == "INFO")
    return (
        f"Security review the migrated Azure Function for module '{module}' ({language}).\n\n"
        f"## Source location (host has already scanned)\n"
        f"{source_dir}\n\n"
        f"## Automated Scan Results\n"
        f"Found {len(findings)} findings: BLOCK={block}, WARN={warn}, INFO={info}.\n\n"
        f"{_format_findings_table(findings)}\n\n"
        f"## Migrated Source Code\n"
        f"{source_listing}\n\n"
        f"Now do the OWASP-based deep analysis described in your system instructions, "
        f"covering everything regex can't catch (IDOR, auth bypass, race conditions, "
        f"Azure-specific misuse). Return ONLY the markdown body — no surrounding code "
        f"fence, no commentary."
    )


def _inline_source(source_dir: Path, max_files: int = 60) -> str:
    """Inline migrated source files into a single labelled string."""
    if not source_dir.is_dir():
        return ""
    parts: list[str] = []
    files = sorted(
        p for p in source_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )[:max_files]
    for fpath in files:
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            parts.append(f"--- {fpath} ---\n{content}")
        except OSError as e:
            logger.warning(
                "security_reviewer.read_failed",
                extra={"path": str(fpath), "error": str(e)},
            )
    return "\n\n".join(parts)


def _combine_recommendation(
    findings: list[SecurityFinding], llm_recommendation: str,
) -> str:
    """Enforce the floor rule: a deterministic BLOCK can never be APPROVE'd by the LLM.

    This is a security floor: regex BLOCK findings represent things like
    hardcoded AWS access keys or `os.system(` — the LLM may not know better
    than the deterministic pattern, so we let the regex stand. The LLM's
    own verdict still wins for *promotion* (it can BLOCK on logic bugs the
    regex missed), but never for *demotion*.
    """
    has_block = any(f.severity == "BLOCK" for f in findings)
    if has_block:
        return "BLOCKED"
    return llm_recommendation


# ── Agent construction ────────────────────────────────────────────────────────

async def build_security_reviewer_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    """SecurityReviewer-specific factory wrapper."""
    return await build_agent("security-reviewer", run_id, token_provider=token_provider)


# ── A2A handler ───────────────────────────────────────────────────────────────

class SecurityReviewerHandler:
    """A2A handler for SecurityReviewRequest/v1 → SecurityReviewReport/v1."""

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
                    message="SecurityReviewRequest/v1 requires module, language, migrated_source_dir",
                ),
                status=A2AStatus.ERROR,
            )

        src_path = Path(migrated_source_dir)

        # 1. Deterministic regex scan + source inlining
        findings = scan_directory(src_path)
        source_listing = _inline_source(src_path)

        block_count = sum(1 for f in findings if f.severity == "BLOCK")
        warn_count = sum(1 for f in findings if f.severity == "WARN")
        info_count = sum(1 for f in findings if f.severity == "INFO")

        # 2. LLM call — MAF AgentTelemetryLayer creates the child span automatically
        t0 = time.perf_counter()
        user_prompt = _build_user_prompt(
            module=module, language=language, source_dir=str(src_path),
            findings=findings, source_listing=source_listing,
        )
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

        # 3. Recommendation: parse LLM's verdict, then floor to BLOCKED if any
        #    deterministic BLOCK finding exists. The LLM may NOT downgrade.
        llm_rec, confidence, _ = parse_review_output(review_md)
        recommendation = _combine_recommendation(findings, llm_rec)

        # 4. Optional disk sink
        output_dir = payload.get("output_dir")
        if output_dir:
            out_path = Path(output_dir) / module / "security-review.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(review_md, encoding="utf-8")

        report = SecurityReviewReport(
            module=module,
            recommendation=recommendation,
            confidence=confidence,
            block_count=block_count,
            warn_count=warn_count,
            info_count=info_count,
            automated_findings=[asdict(f) for f in findings],
            review_markdown=review_md,
        )

        return A2AResponse.ok(
            request=request,
            payload=report.to_dict(),
            payload_schema=REPORT_SCHEMA,
            latency_ms=latency_ms,
        )


