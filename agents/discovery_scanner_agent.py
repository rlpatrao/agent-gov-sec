"""
Discovery Scanner Agent — Stage 1 of the Discovery pipeline.

Walks a repo's file tree and asks the LLM to produce a structured Inventory
(modules, languages, entrypoints, LOC). Deterministic tree walk runs first;
the LLM interprets ambiguous entry-point boundaries.

A2A schema:
  request:  DiscoveryScanRequest/v1  {repo_id, repo_path, extra_instructions?}
  response: DiscoveryInventory/v1    {inventory: <Inventory JSON>}
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.run_logger import get_run_logger
from agents.config import load_agent_config_cached
from core.discovery_artifacts import CriticReport, Inventory
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("discovery-scanner")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "DiscoveryScanRequest/v1"
RESPONSE_SCHEMA = "DiscoveryInventory/v1"

_TREE_LIMIT = 200
_EXCLUDE_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _list_tree(root: Path) -> list[str]:
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if any(seg in _EXCLUDE_PARTS for seg in p.parts):
            continue
        out.append(str(p.relative_to(root)))
        if len(out) >= _TREE_LIMIT:
            break
    return out


def sanity_check(inv: Inventory, repo_root: Path) -> CriticReport:
    """Deterministic post-check — no LLM."""
    reasons: list[str] = []
    for m in inv.modules:
        handler = repo_root / m.handler_entrypoint
        if not handler.is_file():
            reasons.append(f"handler not found: {m.handler_entrypoint}")
            continue
        ext_to_lang = {".py": "python", ".js": "node", ".ts": "node",
                       ".java": "java", ".cs": "csharp"}
        expected = ext_to_lang.get(handler.suffix.lower())
        if expected and m.language != expected:
            reasons.append(
                f"module {m.id}: language={m.language} but extension implies {expected}"
            )
    return CriticReport(
        verdict="PASS" if not reasons else "FAIL",
        reasons=reasons,
        suggestions=[],
    )


async def build_discovery_scanner_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    return await build_agent("discovery-scanner", run_id, token_provider=token_provider)


class DiscoveryScannerHandler:
    """A2A handler for DiscoveryScanRequest/v1 → DiscoveryInventory/v1."""

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
        repo_id = (payload.get("repo_id") or "").strip()
        repo_path = (payload.get("repo_path") or "").strip()
        extra_instructions = (payload.get("extra_instructions") or "").strip()

        if not repo_id or not repo_path:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="DiscoveryScanRequest/v1 requires repo_id and repo_path"),
                status=A2AStatus.ERROR,
            )

        root = Path(repo_path).resolve()
        if not root.is_dir():
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message=f"repo_path does not exist or is not a directory: {repo_path}"),
                status=A2AStatus.ERROR,
            )

        listing = "\n".join(_list_tree(root))
        now = datetime.now(timezone.utc).isoformat()

        user_prompt = (
            f"repo_path: {root}\n"
            f"discovered_at: {now}\n\n"
            f"## File tree (truncated to {_TREE_LIMIT} entries)\n{listing}\n\n"
            + (f"{extra_instructions}\n\n" if extra_instructions else "")
            + "Return ONLY the JSON object."
        )

        t0 = time.perf_counter()
        llm_response = await self._agent.run(
            user_prompt,
            options={"extra_headers": {
                "x-galaxy-run-id": request.run_id,
                "x-module-id": request.module_id,
            }},
        )
        raw = extract_response_text(llm_response).strip()
        tokens_in, tokens_out = extract_usage(llm_response)
        latency_ms = (time.perf_counter() - t0) * 1000

        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=1, module=repo_id,
                latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            )

        raw = _strip_fences(raw)
        try:
            inv = Inventory.model_validate_json(raw)
        except (ValidationError, Exception) as exc:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="parse_error",
                               message=f"LLM output failed Inventory validation: {exc}"),
                status=A2AStatus.ERROR,
            )

        sanity = sanity_check(inv, root)
        if sanity.verdict == "FAIL":
            return A2AResponse.error(
                request=request,
                error=A2AError(code="sanity_check_failed",
                               message="; ".join(sanity.reasons[:5])),
                status=A2AStatus.ERROR,
            )

        return A2AResponse.ok(
            request=request,
            payload={"inventory": inv.model_dump()},
            payload_schema=RESPONSE_SCHEMA,
            latency_ms=latency_ms,
        )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()
