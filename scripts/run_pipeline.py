"""
End-to-End Pipeline: Discovery → Migration (wave-parallel).

Phase 1 — Discovery  (Scanner → Grapher → BRD → Architect → Stories → WaveScheduler)
  Produces a wave-ordered Backlog of migration work items.

Phase 2 — Migration  (Analyzer → Coder → Tester → Reviewer → SecurityReviewer)
  Runs each wave sequentially; items within a wave run in parallel.
  Migration agents are built fresh per module so governance audit chains
  are correctly isolated.

Usage:
    # Full run (discovery + migration):
    PYTHONPATH=. .venv/bin/python scripts/run_pipeline.py \\
        --repo-path /path/to/aws-repo \\
        --output-root /tmp/pipeline-out \\
        [--codebase-type python_serverless] \\
        [--wave-concurrency 3] \\
        [--fast-stories]

    # Skip discovery, reuse an existing backlog:
    PYTHONPATH=. .venv/bin/python scripts/run_pipeline.py \\
        --repo-path /path/to/aws-repo \\
        --output-root /tmp/pipeline-out \\
        --backlog /tmp/pipeline-out/my-repo/backlog.json

Output layout under <output_root>/<repo_id>/:
    backlog.json              — wave-ordered work items (from discovery)
    modules/<module>/v<N>/    — migrated code, tests, Bicep IaC
    modules/<module>/v<N>/logs/ — per-module RunLogger (orchestration/agents/a2a JSONL)
    pipeline_summary.json     — final wave/module status summary
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ── Platform imports ──────────────────────────────────────────────────────────
from agents._lib.run_logger import RunLogger, get_run_logger, set_run_logger
from agents._lib.wave_scheduler import schedule as wave_schedule
from agents.analyzer_agent import AnalyzerHandler, build_analyzer_agent
from agents.coder_agent import CoderHandler, build_coder_agent
from agents.reviewer_agent import ReviewerHandler, build_reviewer_agent
from agents.security_reviewer_agent import (
    SecurityReviewerHandler, build_security_reviewer_agent,
)
from agents.tester_agent import TesterHandler, build_tester_agent
from core.discovery_artifacts import Backlog, BacklogItem
from core.run_tracer import configure_tracing, pipeline_span
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
from scripts.run_discovery import DiscoveryHandlers, run_discovery
from scripts.run_migration import TESTER_TIMEOUT, migrate_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
)
logger = logging.getLogger("pipeline.orchestrator")

DEFAULT_WAVE_CONCURRENCY = 3


# ── Module output path helper (mirrors run_migration._versioned_output) ───────

def _module_output(output_root: Path, module: str) -> Path:
    """Return a fresh versioned output dir: <root>/modules/<module>/v<N>/"""
    base = output_root / "modules" / module
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while (base / f"v{n}").exists():
        n += 1
    out = base / f"v{n}"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ── Per-module migration (builds fresh agents, runs full pipeline) ────────────

async def _migrate_one(
    item: BacklogItem,
    *,
    run_id: str,
    output_root: Path,
    codebase_type: str,
    rl: RunLogger,
) -> dict:
    """Migrate one BacklogItem.  Returns a status dict."""
    module = item.module
    output_dir = _module_output(output_root, module)
    infra_root = output_dir / "infrastructure"
    infra_root.mkdir(parents=True, exist_ok=True)

    # Per-module RunLogger so logs are isolated under modules/<module>/v<N>/logs/
    module_rl = RunLogger(run_id, logs_root=output_dir / "logs")
    set_run_logger(module_rl)

    logger.info("[wave %d] migrating %s → %s", item.wave, module, output_dir)
    rl.log_phase("start", "migration", module=module,
                 wave=item.wave, codebase_type=codebase_type)

    # Build agents fresh — governance audit chains must be per-module
    bundles = {}
    try:
        bundles["analyzer"] = await build_analyzer_agent(run_id)
        bundles["coder"] = await build_coder_agent(
            run_id, sandbox_root=output_dir, codebase_type=codebase_type,
        )
        bundles["tester"] = await build_tester_agent(
            run_id, sandbox_root=output_dir, timeout_seconds=TESTER_TIMEOUT,
        )
        bundles["reviewer"] = await build_reviewer_agent(run_id)
        bundles["security_reviewer"] = await build_security_reviewer_agent(run_id)
    except Exception as exc:
        logger.error("[%s] agent build failed: %s", module, exc)
        return {"module": module, "wave": item.wave, "status": "agent_build_failed",
                "error": str(exc)}

    handlers = {
        "analyzer":          AnalyzerHandler(bundles["analyzer"].agent,
                                             nhi_id=bundles["analyzer"].nhi_id),
        "coder":             CoderHandler(bundles["coder"].agent,
                                          nhi_id=bundles["coder"].nhi_id),
        "tester":            TesterHandler(bundles["tester"].agent,
                                           nhi_id=bundles["tester"].nhi_id),
        "reviewer":          ReviewerHandler(bundles["reviewer"].agent,
                                              nhi_id=bundles["reviewer"].nhi_id),
        "security_reviewer": SecurityReviewerHandler(bundles["security_reviewer"].agent,
                                                      nhi_id=bundles["security_reviewer"].nhi_id),
    }

    t0 = time.perf_counter()
    try:
        result = await migrate_module(
            module=module,
            language=item.language,
            codebase_type=codebase_type,
            source_dir=str(Path(item.source_paths[0]).parent) if item.source_paths else "",
            source_paths=item.source_paths,
            output_root=str(output_dir),
            infra_root=str(infra_root),
            run_id=run_id,
            handlers=handlers,
        )
        result["wave"] = item.wave
        result["output_dir"] = str(output_dir)
        elapsed = time.perf_counter() - t0
        rl.log_phase("end", "migration", module=module,
                     wave=item.wave, status=result.get("status", "ok"),
                     latency_ms=round(elapsed * 1000, 1))
        return result
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error("[%s] migration raised: %s", module, exc)
        rl.log_phase("end", "migration", module=module,
                     wave=item.wave, status="error", latency_ms=round(elapsed * 1000, 1))
        return {"module": module, "wave": item.wave, "status": "error", "error": str(exc)}
    finally:
        for bundle in bundles.values():
            try:
                await bundle.pg_backend.flush_async()
                await bundle.pg_backend.verify_chain()
                bundle.audit_logger.flush()
                await bundle.pg_backend.close()
            except Exception as exc:
                logger.warning("[%s] backend.flush_error: %s", module, exc)


# ── Wave runner ───────────────────────────────────────────────────────────────

async def run_migration_waves(
    backlog: Backlog,
    *,
    run_id: str,
    output_root: Path,
    codebase_type: str,
    wave_concurrency: int,
    rl: RunLogger,
) -> list[dict]:
    """Run all waves sequentially; parallel within each wave."""
    by_wave: dict[int, list[BacklogItem]] = defaultdict(list)
    for item in backlog.items:
        by_wave[item.wave].append(item)

    all_results: list[dict] = []
    semaphore = asyncio.Semaphore(wave_concurrency)

    for wave_num in sorted(by_wave.keys()):
        items = by_wave[wave_num]
        logger.info("Wave %d: %d module(s) — %s",
                    wave_num, len(items), [i.module for i in items])
        rl.log_phase("start", "wave", module="", wave=wave_num,
                     modules=[i.module for i in items])

        async def _bounded(item: BacklogItem) -> dict:
            async with semaphore:
                return await _migrate_one(
                    item, run_id=run_id, output_root=output_root,
                    codebase_type=codebase_type, rl=rl,
                )

        wave_results = await asyncio.gather(*(_bounded(item) for item in items))
        all_results.extend(wave_results)

        passed = sum(1 for r in wave_results if r.get("status") not in ("error", "blocked", "agent_build_failed"))
        failed = len(wave_results) - passed
        logger.info("Wave %d complete — %d/%d passed", wave_num, passed, len(wave_results))
        rl.log_phase("end", "wave", module="", wave=wave_num,
                     passed=passed, failed=failed)

    return all_results


# ── Summary writer ────────────────────────────────────────────────────────────

def _write_summary(
    output_root: Path,
    run_id: str,
    discovery_result: dict,
    migration_results: list[dict],
    elapsed: float,
) -> Path:
    by_wave: dict[int, list[dict]] = defaultdict(list)
    for r in migration_results:
        by_wave[r.get("wave", 0)].append(r)

    summary = {
        "run_id": run_id,
        "elapsed_seconds": round(elapsed, 1),
        "discovery": {
            "modules":       discovery_result.get("modules", 0),
            "stories":       discovery_result.get("stories", 0),
            "backlog_items": discovery_result.get("backlog_items", 0),
            "artifacts":     discovery_result.get("artifacts", {}),
        },
        "migration": {
            "total":   len(migration_results),
            "passed":  sum(1 for r in migration_results
                           if r.get("status") not in ("error", "blocked", "agent_build_failed")),
            "failed":  sum(1 for r in migration_results
                           if r.get("status") in ("error", "blocked", "agent_build_failed")),
            "waves":   {
                str(wave): [
                    {"module": r["module"], "status": r.get("status"),
                     "test_verdict": r.get("test_verdict"), "output_dir": r.get("output_dir")}
                    for r in items
                ]
                for wave, items in sorted(by_wave.items())
            },
        },
    }

    path = output_root / "pipeline_summary.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> int:
    configure_tracing()

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.is_dir():
        logger.error("repo_path not found: %s", repo_path)
        return 1

    run_id = args.run_id or f"pipeline-{uuid.uuid4().hex[:8]}"
    repo_id = args.repo_id or repo_path.name
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    logger.info("Pipeline run %s  repo=%s  output=%s", run_id, repo_path, output_root)
    pipeline_rl = RunLogger(run_id, logs_root=output_root / "logs")
    set_run_logger(pipeline_rl)
    t_total = time.perf_counter()

    with pipeline_span(run_id, repo_id):

        # ── Phase 1: Discovery ────────────────────────────────────────────────
        if args.backlog:
            backlog_path = Path(args.backlog)
            logger.info("Skipping discovery — loading backlog from %s", backlog_path)
            backlog = Backlog.model_validate_json(backlog_path.read_text(encoding="utf-8"))
            discovery_result = {
                "modules": len({i.module for i in backlog.items}),
                "stories": len(backlog.items),
                "backlog_items": len(backlog.items),
                "artifacts": {"backlog": str(backlog_path)},
            }
        else:
            logger.info("Phase 1: Discovery")
            pipeline_rl.log_phase("start", "discovery", module=repo_id)

            scanner_bundle  = await build_discovery_scanner_agent(run_id)
            grapher_bundle  = await build_discovery_grapher_agent(run_id)
            brd_bundle      = await build_discovery_brd_agent(run_id)
            architect_bundle = await build_discovery_architect_agent(run_id)
            stories_bundle  = await build_discovery_stories_agent(run_id)

            discovery_handlers = DiscoveryHandlers(
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
                discovery_result = await run_discovery(
                    repo_path=str(repo_path),
                    output_root=str(output_root),
                    run_id=run_id,
                    repo_id=repo_id,
                    handlers=discovery_handlers,
                    fast_stories=args.fast_stories,
                )
            except RuntimeError as exc:
                logger.error("Discovery blocked: %s", exc)
                return 2
            finally:
                for bundle in [scanner_bundle, grapher_bundle, brd_bundle,
                               architect_bundle, stories_bundle]:
                    try:
                        await bundle.pg_backend.flush_async()
                        await bundle.pg_backend.verify_chain()
                        bundle.audit_logger.flush()
                        await bundle.pg_backend.close()
                    except Exception as exc:
                        logger.warning("discovery.backend.flush_error: %s", exc)

            pipeline_rl.log_phase("end", "discovery", module=repo_id,
                                  modules=discovery_result["modules"],
                                  backlog_items=discovery_result["backlog_items"])

            backlog_path = Path(discovery_result["artifacts"]["backlog"])
            backlog = Backlog.model_validate_json(backlog_path.read_text(encoding="utf-8"))

        logger.info("Backlog: %d items across %d wave(s)",
                    len(backlog.items),
                    len({i.wave for i in backlog.items}))

        # ── Determine codebase_type for the migration phase ───────────────────
        if args.codebase_type:
            codebase_type = args.codebase_type
        else:
            from agents._lib.repo_classifier import classify_repo
            clf = classify_repo(str(repo_path))
            codebase_type = clf.codebase_type or "python_serverless"
            logger.info("Classified as '%s' (confidence=%.2f)",
                        codebase_type, clf.confidence)

        # ── Phase 2: Migration (wave-by-wave) ─────────────────────────────────
        logger.info("Phase 2: Migration  codebase_type=%s  wave_concurrency=%d",
                    codebase_type, args.wave_concurrency)
        pipeline_rl.log_phase("start", "migration_phase", module=repo_id,
                               codebase_type=codebase_type,
                               total_items=len(backlog.items))

        migration_results = await run_migration_waves(
            backlog,
            run_id=run_id,
            output_root=output_root,
            codebase_type=codebase_type,
            wave_concurrency=args.wave_concurrency,
            rl=pipeline_rl,
        )

        passed = sum(1 for r in migration_results
                     if r.get("status") not in ("error", "blocked", "agent_build_failed"))
        failed = len(migration_results) - passed
        pipeline_rl.log_phase("end", "migration_phase", module=repo_id,
                               passed=passed, failed=failed)

    elapsed = time.perf_counter() - t_total
    summary_path = _write_summary(output_root, run_id, discovery_result,
                                  migration_results, elapsed)

    logger.info(
        "Pipeline complete in %.1fs — %d/%d modules passed  summary=%s",
        elapsed, passed, len(migration_results), summary_path,
    )
    print(json.dumps({"run_id": run_id, "passed": passed, "failed": failed,
                      "summary": str(summary_path)}, indent=2))
    return 0 if failed == 0 else 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="End-to-end pipeline: Discovery → Migration (wave-parallel)."
    )
    p.add_argument("--repo-path", required=True,
                   help="Absolute path to the AWS source repo")
    p.add_argument("--output-root", default="migrated",
                   help="Root directory for all outputs (default: migrated/)")
    p.add_argument("--run-id", default=None,
                   help="Stable identifier for this run (default: pipeline-<random>)")
    p.add_argument("--repo-id", default=None,
                   help="Repo identifier in artifact paths (default: repo dir name)")
    p.add_argument("--codebase-type", default=None,
                   help="Override codebase type (e.g. python_serverless). "
                        "Default: auto-classify via RepoClassifier.")
    p.add_argument("--wave-concurrency", type=int, default=DEFAULT_WAVE_CONCURRENCY,
                   help=f"Max parallel migrations within a wave (default: {DEFAULT_WAVE_CONCURRENCY})")
    p.add_argument("--fast-stories", action="store_true",
                   help="Use deterministic one-story-per-module fast path in discovery")
    p.add_argument("--backlog", default=None,
                   help="Skip discovery; load an existing backlog.json directly")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse_args())))
