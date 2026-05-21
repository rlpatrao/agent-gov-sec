"""
Discovery Pipeline Orchestrator

Runs the 5-stage Discovery pipeline (Scanner → Grapher → BRD → Architect →
Stories) followed by the deterministic WaveScheduler, producing a Backlog
of migration work items.

Each stage supports:
  - Hash-based disk cache: re-running on the same repo skips unchanged stages
  - 3-attempt self-heal loop: critic rejects kick feedback back to the LLM
  - Structured logging via RunLogger (logs/{run_id}/)
  - OTel tracing via pipeline_span() (all agent spans nest under the root span)

Usage:
    uv run python scripts/run_discovery.py \\
        --repo-path /path/to/aws-repo \\
        --output-root /tmp/discovery-out \\
        [--run-id my-run-001] \\
        [--fast-stories]

Output layout under <output_root>/<repo_id>/:
    inventory.json      — Inventory (module records)
    graph.json          — DependencyGraph + .dot + .mmd
    brd/_system.md      — system BRD
    brd/<module>.json   — per-module BRD
    design/_system.md   — system design
    design/<module>.json — per-module design
    stories.json        — Stories (epics + stories)
    backlog.json        — Backlog (wave-ordered migration items)
    blocked/<stage>.md  — written only when a stage fails all 3 attempts
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from pydantic import ValidationError

# ── Platform imports ──────────────────────────────────────────────────────────
from a2a.envelope import A2ARequest
from agents._lib.critics.brd_critic import critique_brds
from agents._lib.critics.design_critic import critique_designs
from agents._lib.critics.graph_critic import critique_graph
from agents._lib.critics.story_critic import critique_stories
from agents._lib.run_logger import RunLogger, set_run_logger
from agents._lib.wave_scheduler import schedule as wave_schedule
from agents.discovery_architect_agent import (
    DiscoveryArchitectHandler, build_discovery_architect_agent,
)
from agents.discovery_brd_agent import (
    DiscoveryBRDHandler, build_discovery_brd_agent,
)
from agents.discovery_grapher_agent import (
    DiscoveryGrapherHandler, build_discovery_grapher_agent,
)
from agents.discovery_scanner_agent import (
    DiscoveryScannerHandler, build_discovery_scanner_agent,
)
from agents.discovery_stories_agent import (
    DiscoveryStoriesHandler, build_discovery_stories_agent,
)
from core.discovery_artifacts import (
    Backlog, CriticReport, DependencyGraph, Inventory,
    ModuleBRD, ModuleDesign, Stories, SystemBRD, SystemDesign,
)
from core.run_tracer import configure_tracing, pipeline_span

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
)
logger = logging.getLogger("discovery.orchestrator")

MAX_ATTEMPTS = 3
PROMPT_VERSION = "v1"


# ── Artifact path helpers ─────────────────────────────────────────────────────

def _repo_dir(output_root: Path, repo_id: str) -> Path:
    return output_root / repo_id


def _inventory_path(output_root: Path, repo_id: str) -> Path:
    return _repo_dir(output_root, repo_id) / "inventory.json"


def _graph_path(output_root: Path, repo_id: str) -> Path:
    return _repo_dir(output_root, repo_id) / "graph.json"


def _brd_dir(output_root: Path, repo_id: str) -> Path:
    return _repo_dir(output_root, repo_id) / "brd"


def _design_dir(output_root: Path, repo_id: str) -> Path:
    return _repo_dir(output_root, repo_id) / "design"


def _stories_path(output_root: Path, repo_id: str) -> Path:
    return _repo_dir(output_root, repo_id) / "stories.json"


def _backlog_path(output_root: Path, repo_id: str) -> Path:
    return _repo_dir(output_root, repo_id) / "backlog.json"


def _blocked_path(output_root: Path, repo_id: str, stage: str) -> Path:
    return _repo_dir(output_root, repo_id) / "blocked" / f"{stage}.md"


def _hash_file(path: Path) -> str:
    """Return SHA-256 of the sidecar hash file for a stage's artifact."""
    return str(path) + ".hash"


# ── Hash-based disk cache ─────────────────────────────────────────────────────

def hash_inputs(repo_id: str, stage_name: str, parts: list[str]) -> str:
    h = hashlib.sha256()
    h.update(repo_id.encode())
    h.update(b"\0")
    h.update(stage_name.encode())
    h.update(b"\0")
    h.update(PROMPT_VERSION.encode())
    for p in parts:
        h.update(b"\0")
        h.update(p.encode("utf-8", errors="replace"))
    return h.hexdigest()


def _cache_hit(artifact_path: Path, input_hash: str) -> bool:
    hash_path = Path(_hash_file(artifact_path))
    return (
        artifact_path.exists()
        and hash_path.exists()
        and hash_path.read_text(encoding="utf-8").strip() == input_hash
    )


def _write_cache(artifact_path: Path, content: str, input_hash: str) -> None:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(content, encoding="utf-8")
    Path(_hash_file(artifact_path)).write_text(input_hash, encoding="utf-8")


# ── Stage runner (caching + 3-attempt self-heal) ──────────────────────────────

ProduceFn = Callable[[str], Awaitable[str]]
CriticFn = Callable[[str], CriticReport]


async def run_stage(
    *,
    stage_name: str,
    produce: ProduceFn,
    critic: CriticFn,
    artifact_path: Path,
    input_hash: str,
    stage_timeout: Optional[float] = None,
) -> str:
    if _cache_hit(artifact_path, input_hash):
        cached = artifact_path.read_text(encoding="utf-8")
        logger.info("[%s] cache hit", stage_name)
        return cached

    feedback = ""
    last: Optional[CriticReport] = None
    stage_start = time.perf_counter()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info("[%s] attempt %d/%d", stage_name, attempt, MAX_ATTEMPTS)
        attempt_start = time.perf_counter()
        try:
            if stage_timeout is not None:
                result = await asyncio.wait_for(produce(feedback), timeout=stage_timeout)
            else:
                result = await produce(feedback)
        except asyncio.TimeoutError:
            report = CriticReport(
                verdict="FAIL",
                reasons=[f"stage timed out after {stage_timeout}s"],
                suggestions=["Raise the per-stage timeout or investigate the stall."],
            )
            feedback = "\n\n## Critic feedback (apply this):\n" + "\n".join(
                f"- {r}" for r in report.reasons)
            last = report
            logger.warning("[%s] attempt %d timed out (%.1fs)", stage_name, attempt,
                           time.perf_counter() - attempt_start)
            continue

        report = critic(result)
        elapsed = time.perf_counter() - attempt_start
        if report.verdict == "PASS":
            _write_cache(artifact_path, result, input_hash)
            logger.info("[%s] passed on attempt %d (%.1fs, total %.1fs)",
                        stage_name, attempt, elapsed, time.perf_counter() - stage_start)
            return result

        logger.warning("[%s] attempt %d failed critic (%.1fs): %s",
                       stage_name, attempt, elapsed,
                       "; ".join(report.reasons[:3]) or "critic rejected output")
        feedback = (
            "\n\n## Critic feedback (apply this):\n"
            + "\n".join(f"- {r}" for r in report.reasons)
            + ("\n\n### Suggestions\n" + "\n".join(f"- {s}" for s in report.suggestions)
               if report.suggestions else "")
        )
        last = report

    blocked = _blocked_path(artifact_path.parent.parent, artifact_path.parent.name, stage_name)
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text(
        f"# Blocked: stage `{stage_name}`\n\n"
        f"Failed after {MAX_ATTEMPTS} self-heal attempts at "
        f"{datetime.now(timezone.utc).isoformat()}.\n\n"
        f"## Last critic report\n```json\n{last.model_dump_json(indent=2) if last else '{}'}\n```\n",
        encoding="utf-8",
    )
    raise RuntimeError(f"stage {stage_name} blocked after {MAX_ATTEMPTS} attempts")


# ── A2A dispatch helper ───────────────────────────────────────────────────────

def _make_request(
    run_id: str,
    module_id: str,
    payload: dict,
    payload_schema: str,
) -> A2ARequest:
    return A2ARequest(
        run_id=run_id,
        module_id=module_id,
        payload=payload,
        payload_schema=payload_schema,
    )


async def _dispatch(handler, request: A2ARequest, *, stage: str):
    """Call handler.handle(), raise on error status."""
    from a2a.envelope import A2AStatus
    response = await handler.handle(request)
    if response.status != A2AStatus.OK:
        err = response.error
        msg = err.message if err else "unknown error"
        raise RuntimeError(f"[{stage}] agent returned error: {msg}")
    return response


# ── Handlers container ────────────────────────────────────────────────────────

@dataclass
class DiscoveryHandlers:
    scanner: DiscoveryScannerHandler
    grapher: DiscoveryGrapherHandler
    brd: DiscoveryBRDHandler
    architect: DiscoveryArchitectHandler
    stories: DiscoveryStoriesHandler


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_discovery(
    *,
    repo_path: str,
    output_root: str,
    run_id: str,
    repo_id: str,
    handlers: DiscoveryHandlers,
    fast_stories: bool = False,
) -> dict:
    root = Path(repo_path).resolve()
    out = Path(output_root)
    rl = RunLogger(run_id)
    set_run_logger(rl)

    rl.log_phase("start", "discovery", module=repo_id, status="running")

    # ── Stage 1: Scanner ──────────────────────────────────────────────────────
    inv_path = _inventory_path(out, repo_id)
    inv_hash = hash_inputs(repo_id, "scanner", [str(root)])

    async def _produce_scanner(feedback: str) -> str:
        req = _make_request(
            run_id, repo_id,
            payload={"repo_id": repo_id, "repo_path": str(root),
                     "extra_instructions": feedback},
            payload_schema="DiscoveryScanRequest/v1",
        )
        resp = await _dispatch(handlers.scanner, req, stage="scanner")
        inv_dict = resp.payload["inventory"]
        return json.dumps(inv_dict)

    def _critic_scanner(result: str) -> CriticReport:
        try:
            inv = Inventory.model_validate_json(result)
        except ValidationError as exc:
            return CriticReport(verdict="FAIL",
                                reasons=[f"Inventory parse error: {exc}"])
        from agents.discovery_scanner_agent import sanity_check
        return sanity_check(inv, root)

    raw_inv = await run_stage(
        stage_name="scanner", produce=_produce_scanner, critic=_critic_scanner,
        artifact_path=inv_path, input_hash=inv_hash, stage_timeout=120,
    )
    inventory = Inventory.model_validate_json(raw_inv)
    logger.info("scanner: %d modules", len(inventory.modules))

    # ── Stage 2: Grapher ──────────────────────────────────────────────────────
    graph_path = _graph_path(out, repo_id)
    g_hash = hash_inputs(repo_id, "grapher", [raw_inv])

    async def _produce_grapher(feedback: str) -> str:
        req = _make_request(
            run_id, repo_id,
            payload={"repo_id": repo_id, "repo_path": str(root),
                     "inventory_json": raw_inv},
            payload_schema="DiscoveryGraphRequest/v1",
        )
        resp = await _dispatch(handlers.grapher, req, stage="grapher")
        return json.dumps(resp.payload["graph"])

    def _critic_grapher(result: str) -> CriticReport:
        try:
            g = DependencyGraph.model_validate_json(result)
        except ValidationError as exc:
            return CriticReport(verdict="FAIL",
                                reasons=[f"DependencyGraph parse error: {exc}"])
        return critique_graph(g, root, inventory)

    raw_graph = await run_stage(
        stage_name="grapher", produce=_produce_grapher, critic=_critic_grapher,
        artifact_path=graph_path, input_hash=g_hash, stage_timeout=180,
    )
    graph = DependencyGraph.model_validate_json(raw_graph)
    logger.info("grapher: %d nodes, %d edges", len(graph.nodes), len(graph.edges))

    # ── Stage 3: BRD ─────────────────────────────────────────────────────────
    brd_summary_path = _brd_dir(out, repo_id) / "_summary.json"
    b_hash = hash_inputs(repo_id, "brd", [raw_inv, raw_graph])
    cached_brds: list[ModuleBRD] = []
    cached_sys_brd: Optional[SystemBRD] = None

    async def _produce_brd(feedback: str) -> str:
        nonlocal cached_brds, cached_sys_brd
        req = _make_request(
            run_id, repo_id,
            payload={"repo_id": repo_id, "repo_path": str(root),
                     "inventory_json": raw_inv, "graph_json": raw_graph,
                     "extra_instructions": feedback},
            payload_schema="DiscoveryBRDRequest/v1",
        )
        resp = await _dispatch(handlers.brd, req, stage="brd")
        cached_brds = [ModuleBRD.model_validate(m) for m in resp.payload["modules"]]
        cached_sys_brd = SystemBRD.model_validate(resp.payload["system"])
        return json.dumps({"modules": [b.model_dump() for b in cached_brds],
                           "system": cached_sys_brd.model_dump()})

    def _critic_brd(result: str) -> CriticReport:
        if not cached_brds or cached_sys_brd is None:
            payload = json.loads(result)
            brds = [ModuleBRD.model_validate(m) for m in payload["modules"]]
            sys_brd = SystemBRD.model_validate(payload["system"])
        else:
            brds, sys_brd = cached_brds, cached_sys_brd
        return critique_brds(brds, sys_brd, inventory, graph)

    brd_raw = await run_stage(
        stage_name="brd", produce=_produce_brd, critic=_critic_brd,
        artifact_path=brd_summary_path, input_hash=b_hash, stage_timeout=300,
    )
    if not cached_brds or cached_sys_brd is None:
        payload = json.loads(brd_raw)
        cached_brds = [ModuleBRD.model_validate(m) for m in payload["modules"]]
        cached_sys_brd = SystemBRD.model_validate(payload["system"])
    logger.info("brd: %d module BRDs", len(cached_brds))

    # ── Stage 4: Architect ────────────────────────────────────────────────────
    design_summary_path = _design_dir(out, repo_id) / "_summary.json"
    d_hash = hash_inputs(repo_id, "architect", [
        raw_inv, raw_graph,
        json.dumps([b.model_dump() for b in cached_brds]),
    ])
    cached_designs: list[ModuleDesign] = []
    cached_sys_design: Optional[SystemDesign] = None

    async def _produce_design(feedback: str) -> str:
        nonlocal cached_designs, cached_sys_design
        req = _make_request(
            run_id, repo_id,
            payload={
                "repo_id": repo_id,
                "inventory_json": raw_inv,
                "graph_json": raw_graph,
                "module_brds_json": json.dumps([b.model_dump() for b in cached_brds]),
                "system_brd_json": cached_sys_brd.model_dump_json(),
                "extra_instructions": feedback,
            },
            payload_schema="DiscoveryDesignRequest/v1",
        )
        resp = await _dispatch(handlers.architect, req, stage="architect")
        cached_designs = [ModuleDesign.model_validate(m) for m in resp.payload["modules"]]
        cached_sys_design = SystemDesign.model_validate(resp.payload["system"])
        return json.dumps({"modules": [d.model_dump() for d in cached_designs],
                           "system": cached_sys_design.model_dump()})

    def _critic_design(result: str) -> CriticReport:
        if not cached_designs or cached_sys_design is None:
            payload = json.loads(result)
            designs = [ModuleDesign.model_validate(m) for m in payload["modules"]]
            sys_d = SystemDesign.model_validate(payload["system"])
        else:
            designs, sys_d = cached_designs, cached_sys_design
        return critique_designs(designs, sys_d, inventory, graph, cached_brds)

    design_raw = await run_stage(
        stage_name="architect", produce=_produce_design, critic=_critic_design,
        artifact_path=design_summary_path, input_hash=d_hash, stage_timeout=300,
    )
    if not cached_designs or cached_sys_design is None:
        payload = json.loads(design_raw)
        cached_designs = [ModuleDesign.model_validate(m) for m in payload["modules"]]
        cached_sys_design = SystemDesign.model_validate(payload["system"])
    logger.info("architect: %d module designs", len(cached_designs))

    # ── Stage 5: Stories ──────────────────────────────────────────────────────
    stories_path = _stories_path(out, repo_id)
    s_hash = hash_inputs(repo_id, "stories", [
        raw_inv, raw_graph,
        json.dumps([b.model_dump() for b in cached_brds]),
        json.dumps([d.model_dump() for d in cached_designs]),
    ])

    async def _produce_stories(feedback: str) -> str:
        req = _make_request(
            run_id, repo_id,
            payload={
                "repo_id": repo_id,
                "inventory_json": raw_inv,
                "graph_json": raw_graph,
                "module_brds_json": json.dumps([b.model_dump() for b in cached_brds]),
                "system_brd_json": cached_sys_brd.model_dump_json(),
                "module_designs_json": json.dumps([d.model_dump() for d in cached_designs]),
                "system_design_json": cached_sys_design.model_dump_json(),
                "extra_instructions": feedback,
                "fast_path": fast_stories,
            },
            payload_schema="DiscoveryStoriesRequest/v1",
        )
        resp = await _dispatch(handlers.stories, req, stage="stories")
        return json.dumps(resp.payload["stories"])

    def _critic_stories(result: str) -> CriticReport:
        try:
            s = Stories.model_validate_json(result)
        except ValidationError as exc:
            return CriticReport(verdict="FAIL",
                                reasons=[f"Stories parse error: {exc}"])
        return critique_stories(s, inventory)

    stories_raw = await run_stage(
        stage_name="stories", produce=_produce_stories, critic=_critic_stories,
        artifact_path=stories_path, input_hash=s_hash, stage_timeout=300,
    )
    stories_obj = Stories.model_validate_json(stories_raw)
    logger.info("stories: %d epics, %d stories",
                len(stories_obj.epics), len(stories_obj.stories))

    # ── WaveScheduler (deterministic, no agent) ───────────────────────────────
    lang_by_module = {m.id: m.language for m in inventory.modules}
    backlog = wave_schedule(
        stories_obj,
        language_by_module=lang_by_module,
        inventory=inventory,
        graph=graph,
    )
    backlog_path = _backlog_path(out, repo_id)
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    backlog_path.write_text(backlog.model_dump_json(indent=2), encoding="utf-8")
    logger.info("wave_scheduler: %d backlog items across %d waves",
                len(backlog.items),
                max((i.wave for i in backlog.items), default=0))

    rl.log_phase("end", "discovery", module=repo_id, status="ok",
                 modules=len(inventory.modules),
                 stories=len(stories_obj.stories),
                 backlog_items=len(backlog.items))

    return {
        "status": "ok",
        "repo_id": repo_id,
        "stages": ["scanner", "grapher", "brd", "architect", "stories", "wave_scheduler"],
        "artifacts": {
            "inventory": str(inv_path),
            "graph": str(graph_path),
            "brd_dir": str(_brd_dir(out, repo_id)),
            "design_dir": str(_design_dir(out, repo_id)),
            "stories": str(stories_path),
            "backlog": str(backlog_path),
        },
        "modules": len(inventory.modules),
        "stories": len(stories_obj.stories),
        "backlog_items": len(backlog.items),
    }


# ── CLI entry-point ───────────────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> int:
    configure_tracing()

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.is_dir():
        logger.error("repo_path does not exist: %s", repo_path)
        return 1

    run_id = args.run_id or f"discovery-{uuid.uuid4().hex[:8]}"
    repo_id = args.repo_id or repo_path.name
    output_root = Path(args.output_root).resolve()

    logger.info("Starting discovery run %s for repo %s", run_id, repo_path)

    with pipeline_span(run_id, repo_id):
        # Build all 5 agents (they share the same run_id for tracing)
        scanner_bundle  = await build_discovery_scanner_agent(run_id)
        grapher_bundle  = await build_discovery_grapher_agent(run_id)
        brd_bundle      = await build_discovery_brd_agent(run_id)
        architect_bundle = await build_discovery_architect_agent(run_id)
        stories_bundle  = await build_discovery_stories_agent(run_id)

        handlers = DiscoveryHandlers(
            scanner=DiscoveryScannerHandler(
                scanner_bundle.agent, nhi_id=scanner_bundle.nhi_id),
            grapher=DiscoveryGrapherHandler(
                grapher_bundle.agent, nhi_id=grapher_bundle.nhi_id),
            brd=DiscoveryBRDHandler(
                brd_bundle.agent, nhi_id=brd_bundle.nhi_id),
            architect=DiscoveryArchitectHandler(
                architect_bundle.agent, nhi_id=architect_bundle.nhi_id),
            stories=DiscoveryStoriesHandler(
                stories_bundle.agent, nhi_id=stories_bundle.nhi_id),
        )

        try:
            result = await run_discovery(
                repo_path=str(repo_path),
                output_root=str(output_root),
                run_id=run_id,
                repo_id=repo_id,
                handlers=handlers,
                fast_stories=args.fast_stories,
            )
        except RuntimeError as exc:
            logger.error("Discovery pipeline blocked: %s", exc)
            return 2
        finally:
            # Flush all governance backends
            for bundle in [scanner_bundle, grapher_bundle, brd_bundle,
                           architect_bundle, stories_bundle]:
                try:
                    await bundle.pg_backend.flush_async()
                    await bundle.pg_backend.verify_chain()
                    bundle.audit_logger.flush()
                    await bundle.pg_backend.close()
                except Exception as exc:
                    logger.warning("backend.flush_error: %s", exc)

    print(json.dumps(result, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the Discovery pipeline on an AWS repo.")
    p.add_argument("--repo-path", required=True, help="Absolute path to the AWS codebase")
    p.add_argument("--output-root", default="migrated/discovery",
                   help="Root directory for discovery artifacts (default: migrated/discovery)")
    p.add_argument("--run-id", default=None,
                   help="Stable run identifier (default: discovery-<random>)")
    p.add_argument("--repo-id", default=None,
                   help="Repo identifier used in artifact paths (default: repo dir name)")
    p.add_argument("--fast-stories", action="store_true",
                   help="Use deterministic one-story-per-module fast path (no LLM)")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse_args())))
