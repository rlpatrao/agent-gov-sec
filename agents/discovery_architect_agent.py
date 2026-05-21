"""
Discovery Architect Agent — Stage 4 of the Discovery pipeline.

Produces per-module and system-level Azure target design documents (markdown).
Module tasks run in parallel (up to DISCOVERY_ARCHITECT_CONCURRENCY, default 4).

A2A schema:
  request:  DiscoveryDesignRequest/v1  {repo_id, repo_path, inventory_json,
                                        graph_json, module_brds_json,
                                        system_brd_json, extra_instructions?}
  response: DiscoveryDesignReport/v1   {modules: [{module_id, body}],
                                        system: {body}}
"""
from __future__ import annotations

import asyncio
import json
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
from core.discovery_artifacts import (
    DependencyGraph, Inventory, ModuleBRD, ModuleDesign, SystemBRD, SystemDesign,
)
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("discovery-architect")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "DiscoveryDesignRequest/v1"
RESPONSE_SCHEMA = "DiscoveryDesignReport/v1"

DEFAULT_CONCURRENCY = 4


async def build_discovery_architect_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    return await build_agent("discovery-architect", run_id, token_provider=token_provider)


class DiscoveryArchitectHandler:
    """A2A handler for DiscoveryDesignRequest/v1 → DiscoveryDesignReport/v1."""

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
        inventory_json = payload.get("inventory_json") or ""
        graph_json = payload.get("graph_json") or ""
        module_brds_json = payload.get("module_brds_json") or ""
        system_brd_json = payload.get("system_brd_json") or ""
        extra_instructions = (payload.get("extra_instructions") or "").strip()

        if not repo_id or not inventory_json or not graph_json \
           or not module_brds_json or not system_brd_json:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="DiscoveryDesignRequest/v1 requires repo_id, "
                                       "inventory_json, graph_json, module_brds_json, system_brd_json"),
                status=A2AStatus.ERROR,
            )

        try:
            inventory = Inventory.model_validate_json(inventory_json)
            graph = DependencyGraph.model_validate_json(graph_json)
            module_brds = [ModuleBRD.model_validate(m)
                           for m in json.loads(module_brds_json)]
            system_brd = SystemBRD.model_validate_json(system_brd_json)
        except Exception as exc:
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message=f"Payload validation failed: {exc}"),
                status=A2AStatus.ERROR,
            )

        max_concurrency = max(
            1,
            int(os.environ.get("DISCOVERY_ARCHITECT_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
        )
        semaphore = asyncio.Semaphore(max_concurrency)
        by_id = {b.module_id: b for b in module_brds}
        t0 = time.perf_counter()
        total_tokens_in = total_tokens_out = 0

        async def _design_module(module_id: str, brd_body: str,
                                  edges_text: str) -> tuple[ModuleDesign, int, int]:
            async with semaphore:
                logger.info("discovery_architect.designing_module %s", module_id)
                msg = (
                    f"Produce the Azure target design markdown for module `{module_id}`.\n\n"
                    f"## Required sections (## headings):\n"
                    f"- Function Plan\n- Trigger Bindings\n- State Mapping\n"
                    f"- Secrets\n- Identity\n- IaC (Bicep)\n- Observability\n\n"
                    f"## Module BRD\n{brd_body}\n\n"
                    f"## Module edges\n{edges_text}"
                    + (f"\n\n{extra_instructions}" if extra_instructions else "")
                    + "\n\nOutput ONLY the markdown body."
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
                return ModuleDesign(module_id=module_id, body=body), tin, tout

        module_tasks = []
        for m in inventory.modules:
            brd = by_id.get(m.id)
            if brd is None:
                continue
            edges = [e for e in graph.edges if e.src == m.id or e.dst == m.id]
            edges_text = "\n".join(f"- {e.src} -[{e.kind}]-> {e.dst}" for e in edges)
            module_tasks.append(
                asyncio.create_task(
                    _design_module(m.id, brd.body, edges_text)
                )
            )

        sys_msg = (
            "Produce `_system.md` covering Strangler Seams, Anti-Corruption Layers, "
            "and Shared Resource Migration Ordering.\n\n"
            f"## System BRD\n{system_brd.body}\n\nOutput ONLY the markdown body."
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

        design_results = await asyncio.gather(*module_tasks)
        sys_resp = await sys_task

        modules: list[dict] = []
        for design, tin, tout in design_results:
            modules.append(design.model_dump())
            total_tokens_in += tin
            total_tokens_out += tout

        sys_tin, sys_tout = extract_usage(sys_resp)
        total_tokens_in += sys_tin
        total_tokens_out += sys_tout
        system = SystemDesign(body=extract_response_text(sys_resp).strip())

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
