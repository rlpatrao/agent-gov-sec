"""
Discovery BRD Agent — Stage 3 of the Discovery pipeline.

Produces per-module and system-level Business Requirements Documents (markdown).
Module tasks run in parallel (up to DISCOVERY_BRD_CONCURRENCY, default 4).

A2A schema:
  request:  DiscoveryBRDRequest/v1  {repo_id, repo_path, inventory_json, graph_json,
                                     extra_instructions?}
  response: DiscoveryBRDReport/v1   {modules: [{module_id, body}],
                                     system: {body}}
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.run_logger import get_run_logger
from agents.config import load_agent_config_cached
from core.discovery_artifacts import DependencyGraph, Inventory, ModuleBRD, SystemBRD
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("discovery-brd")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "DiscoveryBRDRequest/v1"
RESPONSE_SCHEMA = "DiscoveryBRDReport/v1"

DEFAULT_CONCURRENCY = 4


def _collect_sources(module_dir: Path, max_chars: int = 60_000) -> str:
    chunks: list[str] = []
    used = 0
    if not module_dir.is_dir():
        return ""
    for f in sorted(module_dir.rglob("*.py")):
        text = f.read_text(encoding="utf-8", errors="replace")
        block = f"--- {f} ---\n{text}\n"
        if used + len(block) > max_chars:
            chunks.append(f"--- {f} (truncated) ---\n")
            break
        chunks.append(block)
        used += len(block)
    return "\n".join(chunks)


def _render_edges(edges) -> str:
    return "\n".join(f"- {e.src} -[{e.kind}]-> {e.dst}" for e in edges)


async def build_discovery_brd_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    return await build_agent("discovery-brd", run_id, token_provider=token_provider)


class DiscoveryBRDHandler:
    """A2A handler for DiscoveryBRDRequest/v1 → DiscoveryBRDReport/v1."""

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
        inventory_json = payload.get("inventory_json") or ""
        graph_json = payload.get("graph_json") or ""
        extra_instructions = (payload.get("extra_instructions") or "").strip()

        if not repo_id or not repo_path or not inventory_json or not graph_json:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="DiscoveryBRDRequest/v1 requires repo_id, repo_path, inventory_json, graph_json"),
                status=A2AStatus.ERROR,
            )

        try:
            inventory = Inventory.model_validate_json(inventory_json)
            graph = DependencyGraph.model_validate_json(graph_json)
        except Exception as exc:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message=f"Payload validation failed: {exc}"),
                status=A2AStatus.ERROR,
            )

        root = Path(repo_path).resolve()
        max_concurrency = max(
            1,
            int(os.environ.get("DISCOVERY_BRD_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
        )
        semaphore = asyncio.Semaphore(max_concurrency)
        t0 = time.perf_counter()
        total_tokens_in = total_tokens_out = 0

        async def _extract_module(module_id: str, module_path: Path,
                                   edges_text: str) -> tuple[ModuleBRD, int, int]:
            async with semaphore:
                logger.info("discovery_brd.extracting_module %s", module_id)
                sources = _collect_sources(module_path)
                msg = (
                    f"Write a BRD markdown for module `{module_id}`.\n\n"
                    f"Required sections: Purpose, Triggers, Inputs, Outputs, Business Rules, "
                    f"Side Effects, Error Paths, Non-Functionals, PII/Compliance.\n\n"
                    f"## Module dependency edges\n{edges_text}\n\n"
                    f"## Source\n{sources}\n\n"
                    + (f"{extra_instructions}\n\n" if extra_instructions else "")
                    + "Output ONLY the markdown body."
                )
                resp = await self._agent.run(
                    msg,
                    options={"extra_headers": {
                        "x-galaxy-run-id": request.run_id,
                        "x-module-id": module_id,
                    }},
                )
                body = extract_response_text(resp).strip()
                tin, tout = extract_usage(resp)
                return ModuleBRD(module_id=module_id, body=body), tin, tout

        module_tasks = []
        for m in inventory.modules:
            edges = [e for e in graph.edges if e.src == m.id or e.dst == m.id]
            module_tasks.append(
                asyncio.create_task(
                    _extract_module(m.id, root / m.path, _render_edges(edges))
                )
            )

        sys_msg = (
            "Write `_system.md` summarizing cross-module workflows and shared invariants.\n\n"
            f"## All edges\n{_render_edges(graph.edges)}\n\n"
            "Output ONLY the markdown body."
        )
        sys_task = asyncio.create_task(
            self._agent.run(
                sys_msg,
                options={"extra_headers": {
                    "x-galaxy-run-id": request.run_id,
                    "x-module-id": "system",
                }},
            )
        )

        module_results = await asyncio.gather(*module_tasks)
        sys_resp = await sys_task

        modules: list[dict] = []
        for brd, tin, tout in module_results:
            modules.append(brd.model_dump())
            total_tokens_in += tin
            total_tokens_out += tout

        sys_tin, sys_tout = extract_usage(sys_resp)
        total_tokens_in += sys_tin
        total_tokens_out += sys_tout
        system = SystemBRD(body=extract_response_text(sys_resp).strip())

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=1, module=repo_id,
                latency_ms=latency_ms,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            )

        return A2AResponse.ok(
            request=request,
            payload={"modules": modules, "system": system.model_dump()},
            payload_schema=RESPONSE_SCHEMA,
            latency_ms=latency_ms,
        )
