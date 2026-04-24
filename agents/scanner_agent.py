"""
Scanner Agent (MAF port) — Tier 1, Discovery Pipeline.

Uses Microsoft Agent Framework (MAF) for LLM orchestration and the
agent_os middleware stack for governance. Domain logic (repo traversal,
entry-point detection, output parsing) stays as plain Python.

Rules:
  - Every LLM invocation goes through `agent.run(...)` — middleware stack
    owns input/output validation, policy evaluation, audit, and anomaly.
  - Hash-chained Postgres ledger is mirrored from the audit log as a
    compliance archive (see governance.adapters.postgres_audit_backend).
  - Per-agent NHI identity is tagged onto audit entries via `agent.id`.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient
from agent_os.audit_logger import GovernanceAuditLogger

from a2a import A2ARequest, A2AResponse, a2a_call
from a2a.dispatcher import A2AHandler
from agents.config import load_agent_config_cached
from governance.middleware import build_governance_stack
from nhi_identity import NHIRegistry
from token_provider import TokenProvider

logger = logging.getLogger(__name__)

# All Scanner tunables live in agents/config/scanner.yaml. Pydantic validates
# on load; anything missing or out-of-range raises ConfigError before the
# agent is constructed. Module-level constants are derived here so hot-paths
# can keep using plain identifiers.
_config = load_agent_config_cached("scanner")
AGENT_TYPE = _config.agent_type
MAX_FILE_SCAN_BYTES = _config.max_file_scan_bytes
ALLOWED_A2A_RECIPIENTS = _config.a2a.allowed_recipients
MAX_AST_FILES_PER_DISPATCH = _config.a2a.max_files_per_dispatch


# ── File traversal config ─────────────────────────────────────────────────────

_EXCLUDED_DIRS = {
    ".venv", "venv", ".env", "env",
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "node_modules", "bower_components",
    "dist", "build", "out", "target",
    ".gradle", ".idea", ".vscode",
}

_LANG_EXT = {
    ".py": "python", ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".go": "go", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".rb": "ruby", ".cs": "csharp",
}

_ENTRY_HINTS = (
    'if __name__ == "__main__"',
    "if __name__ == '__main__'",
    "public static void main",
    "@RestController",
    "@SpringBootApplication",
    "@app.route",
    "@router.",
    "FastAPI(",
    "app = FastAPI",
    "lambda_handler",
    "def handler(",
)


# ── Output schema ─────────────────────────────────────────────────────────────

@dataclass
class ScannerOutput:
    module_id: str
    language: str
    file_inventory: list
    entry_points: list
    external_dependencies: list
    dead_files: list
    raw_summary: str
    ast_report: Optional[dict] = None            # populated via A2A call
    ast_conversation_id: Optional[str] = None    # link to A2A conversation in ledger

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are the Scanner agent in the Galaxy migration platform.

Your job: analyse a legacy Java or Python service and produce a structured
inventory of what it does — its live files, entry points, external
dependencies, and dead code.

Rules:
- Focus on business logic and external interfaces only
- Ignore test files, build scripts, generated code unless they reveal dependencies
- Flag unreachable, unused, or deprecated files as dead
- Do not reproduce source code in your output
- Do not make implementation suggestions — that is the Architect's job
- Output must be valid JSON matching the schema provided

Your output is the sole source of truth for all downstream agents.
Accuracy is more important than completeness.
"""

OUTPUT_SCHEMA = """{
  "language": "java|python|...",
  "file_inventory": ["live source files to include"],
  "dead_files": ["unreachable or unused files to exclude"],
  "entry_points": ["Lambda handlers, main classes, FastAPI routers, public APIs"],
  "external_dependencies": ["downstream services, DBs, queues, external APIs"],
  "summary": "2-3 sentence plain English description of what this service does"
}"""


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict | None:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


# ── Deterministic repo traversal (domain logic) ───────────────────────────────

def traverse_repo(repo_path: str) -> dict:
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError(f"Scanner: repo_path is not a directory: {repo_path}")

    files: list[str] = []
    entry_candidates: list[str] = []
    ext_counts: Counter = Counter()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS and not d.startswith(".")]
        for filename in filenames:
            ext = Path(filename).suffix.lower()
            if ext not in _LANG_EXT:
                continue
            full = Path(dirpath) / filename
            rel = str(full.relative_to(root))
            files.append(rel)
            ext_counts[_LANG_EXT[ext]] += 1
            if _looks_like_entry_point(full):
                entry_candidates.append(rel)

    detected_language = ext_counts.most_common(1)[0][0] if ext_counts else "unknown"
    files.sort()
    entry_candidates.sort()
    return {
        "files": files,
        "entry_points": entry_candidates,
        "detected_language": detected_language,
    }


def _looks_like_entry_point(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            head = f.read(MAX_FILE_SCAN_BYTES)
    except OSError:
        return False
    return any(hint in head for hint in _ENTRY_HINTS)


# ── Prompt + output parsing (domain glue) ─────────────────────────────────────

def build_user_prompt(repo_path: str, file_map: dict) -> str:
    return f"""
Analyse this repository inventory and produce a structured JSON summary.

Repository: {repo_path}
Detected language (heuristic): {file_map.get("detected_language", "unknown")}

Files found ({len(file_map.get("files", []))}):
{json.dumps(file_map.get("files", []), indent=2)}

Entry-point candidates (heuristic, refine if needed):
{json.dumps(file_map.get("entry_points", []), indent=2)}

Return JSON only — no prose, no markdown fences.
Schema:
{OUTPUT_SCHEMA}
"""


def parse_scanner_output(raw: str, module_id: str, file_map: dict) -> ScannerOutput:
    parsed = _extract_json_object(raw)
    if parsed is None:
        raise ValueError(
            f"Scanner: model did not return valid JSON. Raw prefix: {raw[:200]!r}"
        )
    return ScannerOutput(
        module_id=module_id,
        language=parsed.get("language", file_map.get("detected_language", "unknown")),
        file_inventory=parsed.get("file_inventory", []),
        entry_points=parsed.get("entry_points", []),
        external_dependencies=parsed.get("external_dependencies", []),
        dead_files=parsed.get("dead_files", []),
        raw_summary=parsed.get("summary", ""),
    )


# ── Agent construction ────────────────────────────────────────────────────────

async def build_scanner_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> tuple[Agent, "PostgresHashChainBackend", "GovernanceAuditLogger"]:
    """Build a Scanner Agent wired to Azure OpenAI + the Galaxy governance stack.

    Returns ``(agent, pg_backend, audit_logger)``. The caller owns the
    lifecycle of ``pg_backend`` — call ``await pg_backend.flush_async()``
    and ``await pg_backend.close()`` at the end of the run.
    """
    tp = token_provider or TokenProvider(
        secret_name="azure-openai-key",
        env_var_fallback="AZURE_OPENAI_KEY",
    )

    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-3-codex")
    # MAF's Azure Responses API uses the literal string "preview", not a dated
    # `YYYY-MM-DD-preview`. Leaving this optional — if AZURE_OPENAI_API_VERSION
    # is set to "preview" or unset, we let the client's default kick in.
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION") or "preview"

    identity = NHIRegistry.get(AGENT_TYPE)
    agent_id = f"{AGENT_TYPE}-{identity.client_id}"

    client = OpenAIChatClient(
        model=deployment,
        api_key=tp.get_api_key(),
        azure_endpoint=endpoint,
        api_version=api_version,
    )

    middleware, pg_backend, audit = await build_governance_stack(
        agent_id=agent_id,
        run_id=run_id,
        enable_rogue_detection=True,
    )

    agent = Agent(
        client=client,
        instructions=SYSTEM_PROMPT,
        name=AGENT_TYPE,
        id=agent_id,
        middleware=middleware,
    )

    logger.info(
        "scanner.agent_built",
        extra={
            "run_id": run_id,
            "agent_id": agent_id,
            "nhi_id": identity.client_id,
            "deployment": deployment,
        },
    )
    return agent, pg_backend, audit


# ── A2A dispatch: Scanner → ASTAnalyzer ───────────────────────────────────────

def _pick_files_for_ast(file_map: dict, scanner_output: ScannerOutput) -> list[str]:
    """Choose which files to hand to the AST agent.

    Priority:
      1. entry points flagged by the LLM (most valuable — they root the graph)
      2. remaining live files from the Scanner's `file_inventory`
    Capped to MAX_AST_FILES_PER_DISPATCH so the envelope stays small.
    """
    seen: set[str] = set()
    picks: list[str] = []

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            picks.append(name)

    for ep in scanner_output.entry_points:
        _add(ep)
    for f in scanner_output.file_inventory:
        _add(f)
        if len(picks) >= MAX_AST_FILES_PER_DISPATCH:
            break
    # Fall back to the raw traversal if the LLM returned nothing useful.
    if not picks:
        for f in file_map.get("files", []):
            _add(f)
            if len(picks) >= MAX_AST_FILES_PER_DISPATCH:
                break
    return picks[:MAX_AST_FILES_PER_DISPATCH]


async def dispatch_ast_analysis(
    *,
    sender_agent_id: str,
    recipient_agent_id: str,
    run_id: str,
    module_id: str,
    repo_path: str,
    file_map: dict,
    scanner_output: ScannerOutput,
    audit: GovernanceAuditLogger,
    handler: A2AHandler,
) -> A2AResponse:
    """Scanner's A2A call into the AST agent.

    The Scanner is the sender (its NHI is on the dispatch audit entry).
    The handler is supplied by the runner — typically
    `ast_agent.ASTAgentHandler(...).handle`.
    """
    files = _pick_files_for_ast(file_map, scanner_output)

    request = A2ARequest.new(
        sender=sender_agent_id,
        recipient=recipient_agent_id,
        run_id=run_id,
        module_id=module_id,
        intent="analyze_ast",
        payload_schema="ASTRequest/v1",
        payload={
            "repo_root": repo_path,
            "files": files,
            "detected_language": scanner_output.language,
        },
    )

    response = await a2a_call(
        request=request,
        handler=handler,
        sender_audit=audit,
        allowed_recipients=ALLOWED_A2A_RECIPIENTS,
    )

    # Merge into ScannerOutput when the reply is ok. Errors stay visible in
    # the ledger via the a2a_reply audit entry — we don't silently drop them.
    if response.is_ok:
        scanner_output.ast_report = response.payload
        scanner_output.ast_conversation_id = response.conversation_id
    else:
        logger.warning(
            "scanner.ast_dispatch_not_ok",
            extra={
                "status": response.status.value,
                "conversation_id": response.conversation_id,
                "reason": (response.payload or {}).get("message", "")[:200],
            },
        )

    return response
