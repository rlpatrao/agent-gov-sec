"""
Coder Agent — stack-aware, TDD-first migration code generator.

Receives analysis + sprint contract + original source via A2A; writes
migrated code, unit tests, and Bicep IaC into a sandboxed output root;
returns a typed CodingReport listing every file produced (verified by
diffing the sandbox before/after the LLM call).

Stack awareness
---------------
`build_coder_agent()` accepts an optional `codebase_type` string.  When
supplied it looks up the `coder_prompt` field in the governance mapping YAML
and passes it as `prompt_file_override` to `build_agent()`.  The shared
coder_rules.md (quality/security/testing gates) is always prepended via
the YAML's `shared_prompt_files` — the per-type prompt only needs to cover
what is different (source stack identity, service mappings, code patterns).

Self-healing across attempts is owned by the orchestrator — this agent just
threads the failure context into the prompt on attempt > 1.
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
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.bicep_tool import validate_bicep
from agents._lib.chunker import chunk_file, needs_chunking
from agents._lib.file_tools import make_apply_patch, make_write_file
from agents._lib.run_logger import get_run_logger
from agents.config import load_agent_config_cached
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("coder")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "CodingRequest/v1"
REPORT_SCHEMA = "CodingReport/v1"

_MAX_SOURCE_FILES = 60
_MAPPING_PATH = Path(__file__).resolve().parent.parent / "governance" / "mappings" / "aws-azure-reference.yaml"


# ── YAML mapping helpers ──────────────────────────────────────────────────────

def _load_mapping() -> dict:
    return yaml.safe_load(_MAPPING_PATH.read_text(encoding="utf-8"))


def _find_repo_type(codebase_type: str) -> Optional[dict]:
    mapping = _load_mapping()
    for rt in mapping.get("repository_types", []):
        if rt.get("codebase_type") == codebase_type:
            return rt
    return None


def _coder_prompt_for_type(codebase_type: str) -> Optional[str]:
    """Return the relative prompt path from the YAML for `codebase_type`, or None."""
    rt = _find_repo_type(codebase_type)
    return rt.get("coder_prompt") if rt else None


def _migration_context_section(codebase_type: str) -> str:
    """Build a '## Migration Context' block from the YAML mapping to inject into the user prompt."""
    rt = _find_repo_type(codebase_type)
    if not rt:
        return ""
    lines = [f"## Migration Context\n- **Stack**: `{codebase_type}`"]
    approach = rt.get("migration_approach") or []
    if approach:
        lines.append("- **Approach**:")
        for step in approach:
            lines.append(f"  - {step}")
    concerns = rt.get("key_concerns") or []
    if concerns:
        lines.append("- **Key concerns**:")
        for c in concerns:
            lines.append(f"  - {c}")
    services = rt.get("target_services") or []
    if services:
        lines.append(f"- **Target Azure services**: {', '.join(services)}")
    return "\n".join(lines)


# ── Public schema ─────────────────────────────────────────────────────────────

@dataclass
class CodingReport:
    module: str
    attempt: int
    output_root: str
    codebase_type: str = ""
    files_written: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    bicep_path: Optional[str] = None
    bicep_validation: Optional[str] = None
    summary_markdown: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Source assembly ───────────────────────────────────────────────────────────

def _collect_source_listing(
    *, source_paths: list[str], source_dir: Optional[str], context_paths: list[str],
) -> str:
    parts: list[str] = []
    primary: list[Path] = []
    if source_paths:
        primary = [Path(p) for p in source_paths[:_MAX_SOURCE_FILES]]
    elif source_dir:
        root = Path(source_dir)
        if root.is_dir():
            primary = sorted(
                p for p in root.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            )[:_MAX_SOURCE_FILES]

    for p in primary:
        if not p.is_file():
            continue
        try:
            if needs_chunking(p):
                chunks = chunk_file(p)
                for i, ch in enumerate(chunks):
                    parts.append(f"--- {p} (chunk {i + 1}/{len(chunks)}) ---\n{ch.content}")
            else:
                parts.append(f"--- {p} ---\n{p.read_text(encoding='utf-8', errors='replace')}")
        except OSError as exc:
            logger.warning("coder.read_failed", extra={"path": str(p), "error": str(exc)})

    if context_paths:
        parts.append(
            "\n## CONTEXT (read-only — anti-corruption boundary)\n"
            "You MAY reference these files; you MUST NOT modify or re-emit them. "
            "If your handler needs helpers from here, INLINE them into "
            "<output_root>/services/ per HARD RULE 3.15.\n"
        )
        for cp in context_paths[:_MAX_SOURCE_FILES]:
            cpath = Path(cp)
            if cpath.is_file():
                try:
                    parts.append(f"--- {cpath} (read-only) ---\n{cpath.read_text(encoding='utf-8', errors='replace')}")
                except OSError:
                    continue

    return "\n\n".join(parts)


def _build_user_prompt(
    *, module: str, language: str, attempt: int,
    codebase_type: str,
    analysis_markdown: Optional[str], sprint_contract_json: Optional[str],
    source_listing: str, previous_failures_json: Optional[str],
    output_root: Path, infra_root: Path,
) -> str:
    sections: list[str] = []

    migration_ctx = _migration_context_section(codebase_type)
    if migration_ctx:
        sections.append(migration_ctx)
    if analysis_markdown:
        sections.append(f"## Analysis\n{analysis_markdown}")
    if sprint_contract_json:
        sections.append(f"## Sprint Contract\n```json\n{sprint_contract_json}\n```")
    sections.append(f"## Original Source\n{source_listing}")
    if previous_failures_json:
        sections.append(
            "## Previous Failure Report\n"
            f"```json\n{previous_failures_json}\n```\n"
            "Apply the self_healing_strategy from each failure. Do NOT re-emit "
            "the same code that produced these errors."
        )

    return (
        f"Migrate module '{module}' ({language}, stack=`{codebase_type}`).\n"
        f"This is attempt {attempt}/3.\n\n"
        + "\n\n".join(sections)
        + "\n\n"
        + f"Write your output to:\n"
        + f"- code + tests: {output_root}/\n"
        + f"- IaC: {infra_root}/main.bicep\n\n"
        + "Use write_file / apply_patch for every file. After all tool calls, "
        + "return a short markdown summary of what you wrote and why. "
        + "The host extracts the file list from the sandbox itself, not from your prose."
    )


# ── Sandbox snapshot ──────────────────────────────────────────────────────────

def _snapshot(root: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if p.is_file():
            try:
                out[str(p)] = p.stat().st_mtime
            except OSError:
                continue
    return out


def _diff_snapshots(before: dict[str, float], after: dict[str, float]) -> tuple[list[str], list[str]]:
    written, modified = [], []
    for path, mtime in after.items():
        if path not in before:
            written.append(path)
        elif mtime > before[path]:
            modified.append(path)
    return sorted(written), sorted(modified)


# ── Agent construction ────────────────────────────────────────────────────────

async def build_coder_agent(
    run_id: str,
    *,
    sandbox_root: str | Path,
    codebase_type: Optional[str] = None,
    extra_allowed_roots: Optional[list[str | Path]] = None,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    """Build the Coder with sandbox-bound write tools.

    `codebase_type` selects the per-stack system prompt from the YAML mapping.
    Falls back to the generic Lambda prompt when None or unmapped.
    The shared coder_rules.md is always prepended (wired via coder.yaml).
    """
    roots: list[str | Path] = [sandbox_root]
    if extra_allowed_roots:
        roots.extend(extra_allowed_roots)
    write_file_tool = make_write_file(roots)
    apply_patch_tool = make_apply_patch(roots)

    prompt_override: Optional[str] = None
    if codebase_type:
        prompt_override = _coder_prompt_for_type(codebase_type)
        if prompt_override:
            logger.info(
                "coder.prompt_selected",
                extra={"codebase_type": codebase_type, "prompt_file": prompt_override},
            )
        else:
            logger.info(
                "coder.prompt_fallback",
                extra={"codebase_type": codebase_type, "reason": "no coder_prompt in mapping"},
            )

    return await build_agent(
        "coder", run_id,
        prompt_file_override=prompt_override,
        token_provider=token_provider,
        tools=[write_file_tool, apply_patch_tool, validate_bicep],
    )


# ── A2A handler ───────────────────────────────────────────────────────────────

class CoderHandler:
    """A2A handler for CodingRequest/v1 → CodingReport/v1."""

    def __init__(self, agent: Agent, *, nhi_id: str = "") -> None:
        self._agent = agent
        self._nhi_id = nhi_id

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
        language = (payload.get("language") or "").strip()
        codebase_type = (payload.get("codebase_type") or "").strip()
        attempt = int(payload.get("attempt") or 1)
        output_root_str = payload.get("output_root")
        if not module or not language or not output_root_str:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="CodingRequest/v1 requires module, language, output_root"),
                status=A2AStatus.ERROR,
            )

        output_root = Path(output_root_str)
        infra_root = Path(payload.get("infra_root") or (output_root / "infrastructure"))
        output_root.mkdir(parents=True, exist_ok=True)
        infra_root.mkdir(parents=True, exist_ok=True)

        source_listing = _collect_source_listing(
            source_paths=list(payload.get("source_paths") or []),
            source_dir=payload.get("source_dir"),
            context_paths=list(payload.get("context_paths") or []),
        )

        before = _snapshot(output_root) | _snapshot(infra_root)

        # LLM call — MAF AgentTelemetryLayer creates the child span automatically
        t0 = time.perf_counter()
        user_prompt = _build_user_prompt(
            module=module, language=language, attempt=attempt,
            codebase_type=codebase_type,
            analysis_markdown=payload.get("analysis_markdown"),
            sprint_contract_json=payload.get("sprint_contract_json"),
            source_listing=source_listing,
            previous_failures_json=payload.get("previous_failures_json"),
            output_root=output_root, infra_root=infra_root,
        )
        llm_response = await self._agent.run(
            user_prompt,
            options={"extra_headers": {
                "x-galaxy-run-id": request.run_id,
                "x-module-id": request.module_id,
            }},
        )
        summary = extract_response_text(llm_response).strip()
        tokens_in, tokens_out = extract_usage(llm_response)

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=attempt, module=module,
                codebase_type=codebase_type, latency_ms=latency_ms,
                tokens_in=tokens_in, tokens_out=tokens_out,
            )

        after = _snapshot(output_root) | _snapshot(infra_root)
        files_written, files_modified = _diff_snapshots(before, after)

        bicep_path: Optional[Path] = None
        bicep_validation: Optional[str] = None
        candidate = infra_root / "main.bicep"
        if candidate.is_file():
            bicep_path = candidate
            try:
                bicep_validation = validate_bicep.func(str(bicep_path))
            except Exception as exc:
                bicep_validation = f"INVALID: validator raised {type(exc).__name__}: {exc}"

        report = CodingReport(
            module=module, attempt=attempt, output_root=str(output_root),
            codebase_type=codebase_type,
            files_written=files_written, files_modified=files_modified,
            bicep_path=str(bicep_path) if bicep_path else None,
            bicep_validation=bicep_validation,
            summary_markdown=summary,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )

        return A2AResponse.ok(
            request=request, payload=report.to_dict(),
            payload_schema=REPORT_SCHEMA, latency_ms=latency_ms,
        )


