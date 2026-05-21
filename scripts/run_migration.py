"""
Galaxy Migration Orchestrator — generic AWS → Azure pipeline.

Directory conventions
---------------------
  Source repos live under:     legacy/<repo-name>/
  Migrated output lands at:    migrated/<repo-name>/v<N>/

Each pipeline run auto-increments the version folder (v1, v2, …), so
previous migration attempts are never overwritten.

Run steps
---------
  Phase 1: Analysis (Analyzer agent)
  Phase 2: Migration (Coder agent, up to MAX_ATTEMPTS)
  Phase 3: Evaluation (Tester agent, up to MAX_ATTEMPTS per Coder attempt)
  Phase 4: Review (Reviewer agent)
  Phase 5: Security review (SecurityReviewer — BLOCKED verdict aborts)
  Phase 6: Bicep validation (done inside Coder handler)

Structured logs are written to migrated/<repo>/v<N>/logs/ (three JSONL files):
  orchestration.jsonl — pipeline phase events
  agents.jsonl        — per-agent LLM call metrics + cost estimates
  a2a.jsonl           — A2A dispatch events with latency and status

Stop conditions
---------------
  - No mapping found for the classified codebase_type → MappingNotFoundError
  - SecurityReviewer verdict == BLOCKED → pipeline aborts with non-zero exit
  - All Coder+Tester attempts exhausted without PASS → logged as PARTIAL

Usage:
    python run_migration.py --source-dir legacy/my_repo [options]

All defaults can be set via .env or environment variables.  See --help.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import time
from pathlib import Path

from dotenv import load_dotenv

# ── Bootstrap ─────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent   # scripts/ -> repo root
load_dotenv(_REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("galaxy.orchestrator")

from core.run_tracer import configure_tracing, pipeline_span  # noqa: E402
configure_tracing(service_name="galaxy-migration")

# ── Agent imports ─────────────────────────────────────────────────────────────

from a2a.envelope import A2ARequest, A2AResponse, A2AStatus
from agents._lib.run_logger import RunLogger, get_run_logger, set_run_logger
from agents.analyzer_agent import (
    AnalyzerHandler,
    MappingNotFoundError,
    build_analyzer_agent,
)
from agents.classifier_agent import ClassifierHandler, build_classifier_agent
from agents.coder_agent import CoderHandler, build_coder_agent
from agents.reviewer_agent import ReviewerHandler, build_reviewer_agent
from agents.security_reviewer_agent import SecurityReviewerHandler, build_security_reviewer_agent
from agents.tester_agent import TesterHandler, build_tester_agent

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_ATTEMPTS = int(os.getenv("MAX_MIGRATION_ATTEMPTS", "3"))
TESTER_TIMEOUT = int(os.getenv("TESTER_TIMEOUT_SECONDS", "120"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _req(
    sender: str,
    recipient: str,
    run_id: str,
    module_id: str,
    intent: str,
    schema: str,
    payload: dict,
) -> A2ARequest:
    return A2ARequest.new(
        sender=sender,
        recipient=recipient,
        run_id=run_id,
        module_id=module_id,
        intent=intent,
        payload_schema=schema,
        payload=payload,
    )


def _versioned_output(base: Path, module_name: str) -> Path:
    """Return the next versioned output path: <base>/<module_name>/v<N>."""
    module_dir = base / module_name
    if module_dir.exists():
        existing = [
            int(d.name[1:]) for d in module_dir.iterdir()
            if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit()
        ]
        n = max(existing, default=0) + 1
    else:
        n = 1
    return module_dir / f"v{n}"


async def _dispatch(handler, request: A2ARequest, *, module: str) -> A2AResponse:
    """Call a handler, log the A2A round-trip to a2a.jsonl, return response."""
    t0 = time.perf_counter()
    resp = await handler.handle(request)
    ms = (time.perf_counter() - t0) * 1000
    rl = get_run_logger()
    if rl:
        rl.log_a2a(
            sender=request.sender, recipient=request.recipient,
            intent=request.intent, latency_ms=ms,
            status="ok" if resp.is_ok else "error",
            payload_schema=request.payload_schema, module=module,
        )
    return resp


# ── Per-module pipeline ────────────────────────────────────────────────────────

async def migrate_module(
    *,
    module: str,
    language: str,
    codebase_type: str,
    source_dir: str,
    source_paths: list[str],
    output_root: str,
    infra_root: str,
    run_id: str,
    handlers: dict,
    sprint_contract_json: str | None = None,
) -> dict:
    """Run the full pipeline for a single module.  Returns a summary dict."""
    module_id = f"{run_id}/{module}"
    results: dict = {"module": module, "codebase_type": codebase_type}
    rl = get_run_logger()
    previous_failures_json: str | None = None

    if rl:
        rl.log_phase("start", "pipeline", module=module, codebase_type=codebase_type)

    # ── Phase 1: Analysis ─────────────────────────────────────────────────────
    logger.info("[%s] Phase 1: Analysis", module)
    if rl:
        rl.log_phase("start", "analysis", module=module)
    analysis_req = _req(
        sender="Orchestrator", recipient="Analyzer",
        run_id=run_id, module_id=module_id,
        intent="analyze_module",
        schema="AnalysisRequest/v1",
        payload={
            "module": module,
            "language": language,
            "codebase_type": codebase_type,
            "source_dir": source_dir,
            "source_paths": source_paths,
            "output_dir": str(Path(output_root) / "analysis"),
        },
    )
    analysis_resp = await _dispatch(handlers["analyzer"], analysis_req, module=module)
    if not analysis_resp.is_ok:
        logger.error("[%s] Analysis failed: %s", module, analysis_resp.payload)
        if rl:
            rl.log_phase("end", "analysis", module=module, status="failed")
        results["status"] = "analysis_failed"
        results["error"] = analysis_resp.payload
        return results

    analysis_payload = analysis_resp.payload
    analysis_md = analysis_payload.get("analysis_markdown", "")
    if rl:
        rl.log_phase("end", "analysis", module=module, status="success",
                     complexity=analysis_payload.get("complexity_level", ""),
                     target_services=analysis_payload.get("target_services", []))
    logger.info(
        "[%s] Analysis done — complexity=%s target_services=%s",
        module,
        analysis_payload.get("complexity_level"),
        analysis_payload.get("target_services"),
    )

    # ── Phase 2+3: Coder → Tester loop ───────────────────────────────────────
    final_verdict = "UNKNOWN"
    final_test_report: dict = {}
    test_dir = str(Path(output_root) / "tests")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info("[%s] Phase 2: Coder attempt %d/%d", module, attempt, MAX_ATTEMPTS)
        if rl:
            rl.log_phase("start", "coder", module=module, attempt=attempt)
        coder_req = _req(
            sender="Orchestrator", recipient="Coder",
            run_id=run_id, module_id=module_id,
            intent="migrate_module",
            schema="CodingRequest/v1",
            payload={
                "module": module,
                "language": language,
                "codebase_type": codebase_type,
                "attempt": attempt,
                "output_root": output_root,
                "infra_root": infra_root,
                "source_dir": source_dir,
                "source_paths": source_paths,
                "analysis_markdown": analysis_md,
                "sprint_contract_json": sprint_contract_json,
                "previous_failures_json": previous_failures_json,
            },
        )
        coder_resp = await _dispatch(handlers["coder"], coder_req, module=module)
        if not coder_resp.is_ok:
            logger.error("[%s] Coder attempt %d failed: %s", module, attempt, coder_resp.payload)
            if rl:
                rl.log_phase("end", "coder", module=module, attempt=attempt, status="failed")
            break

        coder_payload = coder_resp.payload
        if rl:
            rl.log_phase("end", "coder", module=module, attempt=attempt, status="success",
                         files_written=len(coder_payload.get("files_written", [])),
                         files_modified=len(coder_payload.get("files_modified", [])))
        logger.info(
            "[%s] Coder wrote %d files, modified %d",
            module,
            len(coder_payload.get("files_written", [])),
            len(coder_payload.get("files_modified", [])),
        )

        logger.info("[%s] Phase 3: Tester attempt %d/%d", module, attempt, MAX_ATTEMPTS)
        if rl:
            rl.log_phase("start", "tester", module=module, attempt=attempt)
        tester_req = _req(
            sender="Orchestrator", recipient="Tester",
            run_id=run_id, module_id=module_id,
            intent="evaluate_module",
            schema="TestRequest/v1",
            payload={
                "module": module,
                "language": language,
                "attempt": attempt,
                "migrated_source_dir": output_root,
                "test_dir": test_dir,
                "sprint_contract_json": sprint_contract_json,
                "previous_failures_json": previous_failures_json,
                "output_dir": str(Path(output_root) / "eval"),
            },
        )
        tester_resp = await _dispatch(handlers["tester"], tester_req, module=module)
        if not tester_resp.is_ok:
            logger.error("[%s] Tester attempt %d error: %s", module, attempt, tester_resp.payload)
            if rl:
                rl.log_phase("end", "tester", module=module, attempt=attempt, status="error")
            break

        test_payload = tester_resp.payload
        final_verdict = test_payload.get("verdict", "UNKNOWN")
        final_test_report = test_payload
        failures = test_payload.get("failures", [])
        if rl:
            rl.log_phase("end", "tester", module=module, attempt=attempt,
                         status="pass" if final_verdict == "PASS" else "fail",
                         verdict=final_verdict, failure_count=len(failures))
        logger.info(
            "[%s] Tester verdict=%s failures=%d",
            module, final_verdict, len(failures),
        )

        if final_verdict == "PASS":
            break

        if failures and attempt < MAX_ATTEMPTS:
            previous_failures_json = json.dumps({
                "module": module,
                "attempt": attempt,
                "overall_verdict": final_verdict,
                "failures": failures,
            })
        elif attempt == MAX_ATTEMPTS:
            logger.warning("[%s] Exhausted %d attempts, verdict=%s", module, MAX_ATTEMPTS, final_verdict)

    results["test_verdict"] = final_verdict
    results["test_report"] = final_test_report

    # ── Phase 4: Review ───────────────────────────────────────────────────────
    logger.info("[%s] Phase 4: Review", module)
    if rl:
        rl.log_phase("start", "review", module=module)
    reviewer_req = _req(
        sender="Orchestrator", recipient="Reviewer",
        run_id=run_id, module_id=module_id,
        intent="review_module",
        schema="ReviewRequest/v1",
        payload={
            "module": module,
            "language": language,
            "migrated_source_dir": output_root,
            "analysis_markdown": analysis_md,
            "sprint_contract_json": sprint_contract_json,
            "test_results_markdown": final_test_report.get("test_results_markdown", ""),
        },
    )
    reviewer_resp = await _dispatch(handlers["reviewer"], reviewer_req, module=module)
    if reviewer_resp.is_ok:
        review_payload = reviewer_resp.payload
        if rl:
            rl.log_phase("end", "review", module=module, status="success",
                         recommendation=review_payload.get("recommendation", ""))
        logger.info(
            "[%s] Review recommendation=%s confidence=%s",
            module,
            review_payload.get("recommendation"),
            review_payload.get("confidence"),
        )
        results["review"] = review_payload
    else:
        if rl:
            rl.log_phase("end", "review", module=module, status="error")
        logger.warning("[%s] Reviewer error: %s", module, reviewer_resp.payload)

    # ── Phase 5: Security Review ──────────────────────────────────────────────
    logger.info("[%s] Phase 5: Security Review", module)
    if rl:
        rl.log_phase("start", "security_review", module=module)
    sec_req = _req(
        sender="Orchestrator", recipient="SecurityReviewer",
        run_id=run_id, module_id=module_id,
        intent="security_review_module",
        schema="SecurityReviewRequest/v1",
        payload={
            "module": module,
            "language": language,
            "migrated_source_dir": output_root,
        },
    )
    sec_resp = await _dispatch(handlers["security_reviewer"], sec_req, module=module)
    if sec_resp.is_ok:
        sec_payload = sec_resp.payload
        sec_verdict = sec_payload.get("recommendation", "UNKNOWN")
        if rl:
            rl.log_phase("end", "security_review", module=module, status="success",
                         recommendation=sec_verdict)
        logger.info("[%s] SecurityReview recommendation=%s", module, sec_verdict)
        results["security_review"] = sec_payload
        if sec_verdict == "BLOCKED":
            logger.error(
                "[%s] BLOCKED by SecurityReviewer — blocking issues: %s",
                module,
                sec_payload.get("blocking_issues", []),
            )
            if rl:
                rl.log_phase("end", "pipeline", module=module, status="blocked")
            results["status"] = "blocked"
            return results
    else:
        if rl:
            rl.log_phase("end", "security_review", module=module, status="error")
        logger.warning("[%s] SecurityReviewer error: %s", module, sec_resp.payload)

    results["status"] = "completed" if final_verdict == "PASS" else "partial"
    if rl:
        rl.log_phase("end", "pipeline", module=module, status=results["status"],
                     test_verdict=final_verdict)
    return results


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    source_dir: Path,
    output_base: Path,
    run_id: str,
    module: str | None,
    codebase_type_override: str | None,
    language_override: str | None,
) -> int:
    """Top-level async pipeline.  Returns exit code."""

    # ── Step 0: Classify repo (ClassifierAgent — deterministic + LLM fallback) ──
    if codebase_type_override:
        codebase_type = codebase_type_override
        classifier_confidence = 1.0
        classifier_method = "override"
        logger.info("codebase_type overridden to '%s'", codebase_type)
    else:
        logger.info("Classifying repo at %s ...", source_dir)
        classifier_bundle = await build_classifier_agent(run_id)
        clf_handler = ClassifierHandler(
            classifier_bundle.agent, nhi_id=classifier_bundle.nhi_id,
        )
        clf_req = _req(
            sender="Orchestrator", recipient="Classifier",
            run_id=run_id, module_id=run_id,
            intent="classify_repo",
            schema="ClassifyRepoRequest/v1",
            payload={"repo_path": str(source_dir)},
        )
        clf_resp = await _dispatch(clf_handler, clf_req, module="__classify__")
        try:
            await classifier_bundle.pg_backend.flush_async()
            await classifier_bundle.pg_backend.verify_chain()
            classifier_bundle.audit_logger.flush()
            await classifier_bundle.pg_backend.close()
        except Exception as exc:
            logger.warning("classifier.backend.flush_error: %s", exc)

        if not clf_resp.is_ok:
            err = clf_resp.error
            logger.error("Classification failed: %s", err.message if err else clf_resp.payload)
            return 2

        clf_payload = clf_resp.payload
        codebase_type = clf_payload["codebase_type"]
        classifier_confidence = clf_payload["confidence"]
        classifier_method = clf_payload["method"]
        logger.info(
            "Classified as '%s' (confidence=%.2f, method=%s). Top signals: %s",
            codebase_type,
            classifier_confidence,
            classifier_method,
            clf_payload.get("signals_matched", {}).get(codebase_type, [])[:5],
        )

    language = language_override or _infer_language(codebase_type)
    module_name = module or source_dir.name

    # ── Versioned output: migrated/<module>/v<N>/ ─────────────────────────────
    output_root = _versioned_output(output_base, module_name)
    output_root.mkdir(parents=True, exist_ok=True)
    infra_root = output_root / "infrastructure"
    infra_root.mkdir(parents=True, exist_ok=True)
    sandbox_root = output_root

    logger.info("Output root: %s", output_root)

    # ── Structured logging ────────────────────────────────────────────────────
    rl = RunLogger(run_id, logs_root=output_root / "logs")
    set_run_logger(rl)
    logger.info("Structured logs: %s", rl.log_dir)

    # Pass token_provider=None so _resolve_egress in _base.py creates the
    # right provider for the active egress mode (APIM key vs AOAI key).
    logger.info("Building agents for run_id=%s ...", run_id)
    analyzer_bundle = await build_analyzer_agent(run_id)
    coder_bundle = await build_coder_agent(
        run_id, sandbox_root=sandbox_root, codebase_type=codebase_type,
    )
    tester_bundle = await build_tester_agent(run_id, sandbox_root=sandbox_root,
                                             timeout_seconds=TESTER_TIMEOUT)
    reviewer_bundle = await build_reviewer_agent(run_id)
    sec_bundle = await build_security_reviewer_agent(run_id)

    handlers = {
        "analyzer":          AnalyzerHandler(analyzer_bundle.agent, nhi_id=analyzer_bundle.nhi_id),
        "coder":             CoderHandler(coder_bundle.agent, nhi_id=coder_bundle.nhi_id),
        "tester":            TesterHandler(tester_bundle.agent, nhi_id=tester_bundle.nhi_id),
        "reviewer":          ReviewerHandler(reviewer_bundle.agent, nhi_id=reviewer_bundle.nhi_id),
        "security_reviewer": SecurityReviewerHandler(sec_bundle.agent, nhi_id=sec_bundle.nhi_id),
    }

    start = time.perf_counter()
    with pipeline_span(run_id, module_name):
        result = await migrate_module(
            module=module_name,
            language=language,
            codebase_type=codebase_type,
            source_dir=str(source_dir),
            source_paths=[],
            output_root=str(output_root),
            infra_root=str(infra_root),
            run_id=run_id,
            handlers=handlers,
        )
    elapsed = time.perf_counter() - start

    _write_run_summary(output_root, run_id, result, elapsed, codebase_type,
                       classifier_confidence, classifier_method)

    status = result.get("status", "unknown")
    verdict = result.get("test_verdict", "UNKNOWN")
    logger.info(
        "Pipeline complete in %.1fs — status=%s test_verdict=%s output=%s",
        elapsed, status, verdict, output_root,
    )

    if status == "blocked":
        return 1
    return 0


def _infer_language(codebase_type: str) -> str:
    _MAP = {
        "python_serverless": "python",
        "typescript_serverless": "typescript",
        "node_serverless": "javascript",
        "java_serverless": "java",
        "java_spring_boot": "java",
        "ecs_docker": "python",
        "dotnet_serverless": "csharp",
        "frontend_spa": "javascript",
        "php_web_app": "php",
        "iac_terraform": "hcl",
    }
    return _MAP.get(codebase_type, "python")


def _write_run_summary(
    output_root: Path,
    run_id: str,
    result: dict,
    elapsed: float,
    codebase_type: str,
    classifier_confidence: float,
    classifier_method: str = "deterministic",
) -> None:
    summary = {
        "run_id": run_id,
        "codebase_type": codebase_type,
        "classifier_confidence": classifier_confidence,
        "classifier_method": classifier_method,
        "elapsed_seconds": round(elapsed, 2),
        **result,
    }
    summary_path = output_root / "run-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("Run summary written to %s", summary_path)


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Galaxy Migration Orchestrator — AWS → Azure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Directory conventions:\n"
            "  Source repos:  legacy/<repo-name>/\n"
            "  Output:        migrated/<repo-name>/v<N>/   (auto-versioned)\n"
        ),
    )
    p.add_argument(
        "--source-dir",
        default=os.getenv("SOURCE_DIR", ""),
        help="Path to the source repository to migrate (env: SOURCE_DIR). "
             "Relative paths are resolved against cwd; place repos under legacy/.",
    )
    p.add_argument(
        "--output-dir",
        default=os.getenv("MIGRATED_DIR", "migrated"),
        help="Base output directory; versioned subfolder is auto-created "
             "(env: MIGRATED_DIR, default: migrated/)",
    )
    p.add_argument(
        "--module",
        default=os.getenv("MODULE_NAME", ""),
        help="Logical module name (defaults to source-dir basename)",
    )
    p.add_argument(
        "--codebase-type",
        default=os.getenv("CODEBASE_TYPE", ""),
        help="Override the auto-classified codebase_type (env: CODEBASE_TYPE)",
    )
    p.add_argument(
        "--language",
        default=os.getenv("LANGUAGE", ""),
        help="Override the inferred language (env: LANGUAGE)",
    )
    p.add_argument(
        "--run-id",
        default=os.getenv("GALAXY_RUN_ID", f"run-{int(time.time())}"),
        help="Unique run identifier for tracing (env: GALAXY_RUN_ID)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    if not args.source_dir:
        print("ERROR: --source-dir (or SOURCE_DIR env var) is required", file=sys.stderr)
        return 2

    source_dir = Path(args.source_dir).resolve()
    if not source_dir.is_dir():
        print(f"ERROR: source directory not found: {source_dir}", file=sys.stderr)
        return 2

    output_base = Path(args.output_dir).resolve()

    return asyncio.run(
        run_pipeline(
            source_dir=source_dir,
            output_base=output_base,
            run_id=args.run_id,
            module=args.module or None,
            codebase_type_override=args.codebase_type or None,
            language_override=args.language or None,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
