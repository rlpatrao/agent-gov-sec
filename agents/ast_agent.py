"""
AST Analyzer Agent (MAF port) — Tier 1 deep-analysis pair of Scanner.

Receives an A2ARequest from Scanner with a file list, runs a deterministic
tree-sitter extraction, and asks an LLM to produce an architecture +
risk summary grounded in the extracted structured facts. Returns an
A2AResponse carrying an `ASTReport/v1` payload.

Mirrors `agents.scanner_agent`:
  - own NHI (ASTAnalyzer)
  - own MAF client and middleware stack
  - deterministic domain glue (tree-sitter) runs before the LLM
  - `agent.run(...)` is the one and only LLM call, so governance,
    anomaly detection, and audit fire on it automatically
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from typing import Optional

from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents.ast_parser import ASTFindings, extract_ast
from agents.config import load_agent_config_cached
from governance.middleware import build_governance_stack
from nhi_identity import NHIRegistry
from token_provider import TokenProvider

logger = logging.getLogger(__name__)

# Tunables in agents/config/ast_analyzer.yaml. The per-file byte cap and
# the inbound dispatcher deadline are config-driven; the prompt-sampling
# caps below are internal implementation details (shape of the LLM prompt)
# and stay in code for now.
_config = load_agent_config_cached("ast-analyzer")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "ASTRequest/v1"
REPORT_SCHEMA = "ASTReport/v1"

# Ceilings that cap what the LLM sees. The parser already drops files
# larger than `max_file_scan_bytes` from the YAML; these cap the prompt.
_MAX_FILES_PER_REQUEST = _config.a2a.max_files_per_dispatch or 40
_MAX_SYMBOLS_IN_PROMPT = 80
_MAX_EDGES_IN_PROMPT = 60
_MAX_FINDINGS_IN_PROMPT = 30


# ── Public response schema ────────────────────────────────────────────────────

@dataclass
class ASTReport:
    """Structured body carried in A2AResponse.payload for status=ok replies."""
    language: str
    files_analyzed: int
    files_skipped: int
    symbol_count: int
    route_count: int
    db_call_count: int
    finding_count: int
    architecture_summary: str         # LLM-generated narrative
    risks: list = field(default_factory=list)      # [{"severity", "title", "evidence"}, ...]
    routes: list = field(default_factory=list)     # raw Route records
    db_calls: list = field(default_factory=list)   # raw DBCall records
    top_findings: list = field(default_factory=list)   # raw Finding records

    def to_dict(self) -> dict:
        return asdict(self)


# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are the ASTAnalyzer agent in the Galaxy migration platform.

You receive a list of extracted AST facts produced by a deterministic
tree-sitter parser — symbols, call edges, routes, DB calls, and static
findings. Your job is to:

  1. Produce a concise architecture summary (3-6 sentences) describing
     the service's shape: what it exposes, what it persists, and how
     components compose.
  2. Rank the top 5 migration risks, each with severity, title, and
     one-sentence evidence grounded in the facts you were given.

Rules:
  - Ground every claim in the facts you received. Do NOT invent
    routes, DB calls, or call edges that aren't in the input.
  - Do NOT reproduce raw source code — the facts already include
    line numbers and short snippets.
  - Your output must be valid JSON matching the schema.

Severity values: "low" | "medium" | "high".
"""

OUTPUT_SCHEMA = """{
  "architecture_summary": "3-6 sentences describing the service shape",
  "risks": [
    {"severity": "high|medium|low", "title": "...", "evidence": "..."}
  ]
}"""


# ── Prompt building ───────────────────────────────────────────────────────────

def _build_user_prompt(findings: ASTFindings) -> str:
    """Compose the LLM prompt from the extractor output.

    We deliberately hand the LLM a *sampled* view so prompt length stays
    bounded for large scans. The full structured facts are returned
    verbatim in the A2AResponse regardless of the LLM's summary.
    """
    symbols = [asdict(s) for s in findings.symbols[:_MAX_SYMBOLS_IN_PROMPT]]
    edges = [asdict(e) for e in findings.call_edges[:_MAX_EDGES_IN_PROMPT]]
    routes = [asdict(r) for r in findings.routes]
    db_calls = [asdict(d) for d in findings.db_calls]
    static_findings = [asdict(f) for f in findings.findings[:_MAX_FINDINGS_IN_PROMPT]]

    return f"""
Analyse this AST extraction and produce a structured JSON summary.

Language: {findings.language}
Files analyzed: {findings.files_analyzed}   Files skipped: {findings.files_skipped}

Symbols ({len(findings.symbols)} total, first {len(symbols)} shown):
{json.dumps(symbols, indent=2)}

Call edges (first {len(edges)} of {len(findings.call_edges)}):
{json.dumps(edges, indent=2)}

Routes ({len(routes)}):
{json.dumps(routes, indent=2)}

DB call sites ({len(db_calls)}):
{json.dumps(db_calls, indent=2)}

Static findings (first {len(static_findings)} of {len(findings.findings)}):
{json.dumps(static_findings, indent=2)}

Return JSON only, matching:
{OUTPUT_SCHEMA}
"""


def _extract_json_object(text: str) -> Optional[dict]:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _extract_text(response) -> str:
    """Mirror of run_scanner._extract_text — keep the agent loosely coupled
    to MAF response shape changes."""
    if hasattr(response, "text") and response.text:
        return response.text
    if hasattr(response, "messages"):
        for m in response.messages:
            if hasattr(m, "text") and m.text:
                return m.text
            if hasattr(m, "content"):
                return str(m.content)
    return str(response)


# ── Agent construction ────────────────────────────────────────────────────────

async def build_ast_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> tuple[Agent, "PostgresHashChainBackend", "GovernanceAuditLogger"]:
    """Build the AST agent with its own governance stack and NHI.

    Caller owns the pg_backend lifecycle (flush + close at end of run).
    """
    tp = token_provider or TokenProvider(
        secret_name="azure-openai-key",
        env_var_fallback="AZURE_OPENAI_KEY",
    )

    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-3-codex")
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
        "ast_agent.built",
        extra={
            "run_id": run_id,
            "agent_id": agent_id,
            "nhi_id": identity.client_id,
            "deployment": deployment,
        },
    )
    return agent, pg_backend, audit


# ── A2A handler ───────────────────────────────────────────────────────────────

class ASTAgentHandler:
    """Wraps a built AST agent so it can serve A2A requests.

    The handler:
      - validates the request payload matches ASTRequest/v1
      - runs the deterministic extractor
      - asks the LLM for a summary + risks
      - returns an A2AResponse with an ASTReport payload

    Construct once per run; dispatcher.a2a_call invokes `.handle(request)`.
    """

    def __init__(self, agent: Agent, run_tracer=None, nhi_id: str = "") -> None:
        self._agent = agent
        self._run_tracer = run_tracer    # RunTracer or None
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
        repo_root = payload.get("repo_root")
        files = payload.get("files") or []

        if not repo_root or not isinstance(files, list):
            return A2AResponse.error(
                request=request,
                error=A2AError(
                    code="invalid_payload",
                    message="ASTRequest/v1 requires repo_root:str and files:list[str]",
                ),
                status=A2AStatus.ERROR,
            )

        if len(files) > _MAX_FILES_PER_REQUEST:
            logger.info(
                "ast_agent.file_cap_applied",
                extra={"requested": len(files), "kept": _MAX_FILES_PER_REQUEST},
            )
            files = files[:_MAX_FILES_PER_REQUEST]

        # 1. Deterministic parse — no LLM, no network
        findings = extract_ast(repo_root, files)

        # 2. LLM summarisation — wrapped in a RunTracer agent_span so the AST
        #    span is a child of the enclosing A2A dispatch span
        span_cm = (
            self._run_tracer.agent_span(
                agent_type=AGENT_TYPE, attempt=1, nhi_id=self._nhi_id,
            )
            if self._run_tracer is not None
            else _null_cm()
        )
        with span_cm as span:
            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("galaxy.files_analyzed", findings.files_analyzed)
                span.set_attribute("galaxy.routes_found", len(findings.routes))

            user_prompt = _build_user_prompt(findings)
            llm_response = await self._agent.run(user_prompt)
            raw = _extract_text(llm_response)

        parsed = _extract_json_object(raw) or {}
        architecture_summary = (parsed.get("architecture_summary") or "").strip()
        risks = parsed.get("risks") or []

        report = ASTReport(
            language=findings.language,
            files_analyzed=findings.files_analyzed,
            files_skipped=findings.files_skipped,
            symbol_count=len(findings.symbols),
            route_count=len(findings.routes),
            db_call_count=len(findings.db_calls),
            finding_count=len(findings.findings),
            architecture_summary=architecture_summary,
            risks=risks[:10],
            routes=[asdict(r) for r in findings.routes],
            db_calls=[asdict(d) for d in findings.db_calls],
            top_findings=[asdict(f) for f in findings.findings[:_MAX_FINDINGS_IN_PROMPT]],
        )

        return A2AResponse.ok(
            request=request,
            payload=report.to_dict(),
            payload_schema=REPORT_SCHEMA,
            latency_ms=0.0,    # dispatcher stamps the wall-clock latency
        )


class _null_cm:
    def __enter__(self):
        return None
    def __exit__(self, exc_type, exc, tb):
        return False
