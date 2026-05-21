"""
Discovery Stories Agent — Stage 5 of the Discovery pipeline.

Decomposes BRDs and designs into epics and stories with a dependency DAG.
Fast-path mode (DISCOVERY_FAST_STORIES=1) generates one story per module
deterministically without an LLM call.

A2A schema:
  request:  DiscoveryStoriesRequest/v1  {repo_id, inventory_json, graph_json,
                                         module_brds_json, system_brd_json,
                                         module_designs_json, system_design_json,
                                         extra_instructions?, fast_path?}
  response: DiscoveryStories/v1         {stories: <Stories JSON>}
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional

from pydantic import ValidationError

from agent_framework import Agent

from a2a.envelope import A2AError, A2ARequest, A2AResponse, A2AStatus
from agents._base import AgentBundle, build_agent, extract_response_text, extract_usage
from agents._lib.run_logger import get_run_logger
from agents.config import load_agent_config_cached
from core.discovery_artifacts import (
    AcceptanceCriterion, DependencyGraph, Epic, Inventory,
    ModuleBRD, ModuleDesign, Story, Stories, SystemBRD, SystemDesign,
)
from core.token_provider import TokenProvider

logger = logging.getLogger(__name__)

_config = load_agent_config_cached("discovery-stories")
AGENT_TYPE = _config.agent_type
REQUEST_SCHEMA = "DiscoveryStoriesRequest/v1"
RESPONSE_SCHEMA = "DiscoveryStories/v1"


def synthesize_stories(inventory: Inventory, graph: DependencyGraph) -> Stories:
    """Deterministic fast-path: one migration story per module."""
    module_ids = [m.id for m in inventory.modules]
    module_set = set(module_ids)
    raw_deps: dict[str, set[str]] = {m.id: set() for m in inventory.modules}
    for edge in graph.edges:
        if edge.src in module_set and edge.dst in module_set and edge.src != edge.dst:
            raw_deps[edge.src].add(edge.dst)

    deps = _break_cycles(raw_deps, module_ids)
    epics: list[Epic] = []
    stories: list[Story] = []

    for module in inventory.modules:
        epic_id = f"E-{module.id}"
        story_id = f"S-{module.id}"
        dep_story_ids = [f"S-{dep}" for dep in sorted(deps.get(module.id, set()))]
        epics.append(Epic(id=epic_id, module_id=module.id,
                          title=f"Migrate {module.id}", story_ids=[story_id]))
        stories.append(Story(
            id=story_id,
            epic_id=epic_id,
            title=f"Migrate {module.id}",
            description=(
                f"Migrate handler `{module.handler_entrypoint}` from AWS Lambda "
                f"to Azure Functions and preserve its source behavior."
            ),
            acceptance_criteria=[
                AcceptanceCriterion(text=f"Azure Function for `{module.id}` is generated."),
                AcceptanceCriterion(text=f"Tests and infrastructure for `{module.id}` are generated."),
                AcceptanceCriterion(text=f"Behavioral contract for `{module.id}` is preserved."),
            ],
            depends_on=dep_story_ids,
            blocks=[],
            estimate="M",
        ))
    return Stories(epics=epics, stories=stories)


def _break_cycles(raw_deps: dict[str, set[str]], module_order: list[str]) -> dict[str, set[str]]:
    kept: dict[str, set[str]] = {m: set() for m in module_order}
    position = {m: i for i, m in enumerate(module_order)}
    for module in module_order:
        for dep in sorted(raw_deps.get(module, set()), key=lambda x: position.get(x, 10**9)):
            if dep not in position:
                continue
            if _reachable(dep, module, kept):
                continue
            kept[module].add(dep)
    return kept


def _reachable(start: str, target: str, deps: dict[str, set[str]]) -> bool:
    stack = [start]
    seen: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(deps.get(cur, set()))
    return False


async def build_discovery_stories_agent(
    run_id: str,
    token_provider: Optional[TokenProvider] = None,
) -> AgentBundle:
    return await build_agent("discovery-stories", run_id, token_provider=token_provider)


class DiscoveryStoriesHandler:
    """A2A handler for DiscoveryStoriesRequest/v1 → DiscoveryStories/v1."""

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
        module_designs_json = payload.get("module_designs_json") or ""
        system_design_json = payload.get("system_design_json") or ""
        extra_instructions = (payload.get("extra_instructions") or "").strip()
        fast_path = payload.get("fast_path") or (os.environ.get("DISCOVERY_FAST_STORIES") == "1")

        required = [repo_id, inventory_json, graph_json]
        if not all(required):
            return A2AResponse.error(
                request=request,
                error=A2AError(code="invalid_payload",
                               message="DiscoveryStoriesRequest/v1 requires repo_id, "
                                       "inventory_json, graph_json"),
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

        t0 = time.perf_counter()
        tokens_in = tokens_out = 0

        if fast_path:
            logger.info("discovery_stories.fast_path repo=%s", repo_id)
            stories = synthesize_stories(inventory, graph)
        else:
            module_brds = [ModuleBRD.model_validate(m)
                           for m in json.loads(module_brds_json)] if module_brds_json else []
            system_brd = SystemBRD.model_validate_json(system_brd_json) if system_brd_json else SystemBRD(body="")
            module_designs = [ModuleDesign.model_validate(m)
                              for m in json.loads(module_designs_json)] if module_designs_json else []
            system_design = SystemDesign.model_validate_json(system_design_json) if system_design_json else SystemDesign(body="")

            msg = (
                "Decompose the migration into epics and stories. "
                "Return ONLY a JSON object with keys: epics, stories.\n\n"
                f"## Inventory modules\n{', '.join(m.id for m in inventory.modules)}\n\n"
                f"## System BRD\n{system_brd.body}\n\n"
                f"## System Design\n{system_design.body}\n\n"
                "## Per-module BRDs\n"
                + "\n\n".join(f"### {b.module_id}\n{b.body}" for b in module_brds) + "\n\n"
                + "## Per-module Designs\n"
                + "\n\n".join(f"### {d.module_id}\n{d.body}" for d in module_designs) + "\n\n"
                + "## Resource edges\n"
                + "\n".join(f"- {e.src} -[{e.kind}]-> {e.dst}" for e in graph.edges) + "\n\n"
                + (f"{extra_instructions}\n\n" if extra_instructions else "")
                + "Rules:\n"
                + "- At least one epic per module.\n"
                + "- Every story has at least one acceptance_criteria entry.\n"
                + "- depends_on must reference story ids that exist in this output.\n"
                + "- The dependency subgraph must be acyclic.\n"
            )
            llm_response = await self._agent.run(
                msg,
                options={"extra_headers": {
                    "x-galaxy-run-id": request.run_id,
                    "x-module-id": repo_id,
                }},
            )
            raw = _strip_fences(extract_response_text(llm_response).strip())
            tokens_in, tokens_out = extract_usage(llm_response)
            try:
                stories = Stories.model_validate_json(raw)
            except (ValidationError, Exception) as exc:
                return A2AResponse.error(
                    request=request,
                    error=A2AError(code="parse_error",
                                   message=f"LLM output failed Stories validation: {exc}"),
                    status=A2AStatus.ERROR,
                )

        latency_ms = (time.perf_counter() - t0) * 1000
        rl = get_run_logger()
        if rl:
            rl.log_agent(
                agent=AGENT_TYPE, attempt=1, module=repo_id,
                latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            )

        return A2AResponse.ok(
            request=request,
            payload={"stories": json.loads(stories.model_dump_json())},
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
