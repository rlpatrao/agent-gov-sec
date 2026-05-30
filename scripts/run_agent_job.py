"""
scripts/run_agent_job.py

Container App Job entry point — runs exactly one agent per invocation.
Reads artifacts from the shared Azure Files mount (/data/runs/<run_id>/)
and writes this agent's output back to the same tree.

Environment variables (set by the Container App Job definition):
  AGENT_TYPE   — e.g. "Analyzer", "Coder", "Tester", "Reviewer", "SecurityReviewer"
  RUN_ID       — unique pipeline run identifier
  MODULE_ID    — e.g. "aws_lambda"
  DATA_ROOT    — mount point for Azure Files share (default: /data)

All other config (.env-style vars) is read from the Azure Files mount at
/data/.env so the same .env used locally can be shared across all jobs without
baking secrets into the image.

Data layout under /data/runs/<run_id>/:
  source/              <- uploaded by run_pipeline_aca.py before job chain starts
  classifier.json      <- Phase 0 output (ClassificationResult)
  analysis.json        <- Phase 1 output (AnalysisReport)
  code/                <- Phase 2 output (migrated code tree)
  code_report.json     <- Phase 2 metadata (CoderReport)
  test_report.json     <- Phase 3 output (TestReport)
  review.json          <- Phase 4 output (ReviewReport)
  security_report.json <- Phase 5 output (SecurityReport)
  logs/                <- JSONL log files
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sys
import time

# Allow imports from repo root whether invoked inside or outside the container
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# ── Bootstrap ─────────────────────────────────────────────────────────────────

DATA_ROOT  = pathlib.Path(os.environ.get("DATA_ROOT", "/data"))
RUN_ID     = os.environ["RUN_ID"]
MODULE_ID  = os.environ.get("MODULE_ID", "module")
AGENT_TYPE = os.environ["AGENT_TYPE"]

# Load .env from the shared mount so API keys and connection strings are available
_env_file = DATA_ROOT / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)
else:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(f"galaxy.job.{AGENT_TYPE.lower()}")

from core.run_tracer import configure_tracing  # noqa: E402
configure_tracing(service_name=f"galaxy-{AGENT_TYPE.lower()}")

# ── Paths ─────────────────────────────────────────────────────────────────────

RUN_DIR  = DATA_ROOT / "runs" / RUN_ID
LOG_DIR  = RUN_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

from agents._lib.run_logger import RunLogger, set_run_logger  # noqa: E402
_rl = RunLogger(run_id=RUN_ID, log_dir=LOG_DIR)
set_run_logger(_rl)

# ── A2A helpers ───────────────────────────────────────────────────────────────

from a2a.envelope import A2ARequest  # noqa: E402


def _req(sender, recipient, intent, schema, payload) -> A2ARequest:
    return A2ARequest.new(
        sender=sender, recipient=recipient,
        run_id=RUN_ID, module_id=MODULE_ID,
        intent=intent, payload_schema=schema, payload=payload,
    )


def _read_json(path: pathlib.Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Expected artifact not found: {path}")
    return json.loads(path.read_text())


def _write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    logger.info("wrote %s", path)


async def _flush_bundle(bundle) -> None:
    try:
        await bundle.pg_backend.flush_async()
        await bundle.pg_backend.verify_chain()
        bundle.audit_logger.flush()
        await bundle.pg_backend.close()
    except Exception as exc:
        logger.warning("bundle.flush_error: %s", exc)


# ── Per-agent run functions ───────────────────────────────────────────────────

async def run_classifier() -> None:
    from agents.classifier_agent import ClassifierHandler, build_classifier_agent
    bundle = await build_classifier_agent(RUN_ID)
    handler = ClassifierHandler(bundle.agent, nhi_id=bundle.nhi_id)
    req = _req("Orchestrator", "Classifier", "classify_repo",
               "ClassifyRepoRequest/v1", {"repo_path": str(RUN_DIR / "source")})
    resp = await handler.handle(req)
    await _flush_bundle(bundle)
    if not resp.is_ok:
        raise RuntimeError(f"Classifier failed: {resp.error}")
    _write_json(RUN_DIR / "classifier.json", resp.payload)


async def run_analyzer() -> None:
    from agents.analyzer_agent import AnalyzerHandler, build_analyzer_agent
    clf = _read_json(RUN_DIR / "classifier.json")
    source_dir = str(RUN_DIR / "source")
    source_paths = [str(p) for p in pathlib.Path(source_dir).rglob("*") if p.is_file()]
    bundle = await build_analyzer_agent(RUN_ID)
    handler = AnalyzerHandler(bundle.agent, nhi_id=bundle.nhi_id)
    req = _req("Orchestrator", "Analyzer", "analyze_module",
               "AnalysisRequest/v1", {
                   "module": MODULE_ID,
                   "language": clf.get("language", "python"),
                   "codebase_type": clf.get("codebase_type", "generic"),
                   "source_dir": source_dir,
                   "source_paths": source_paths,
                   "output_dir": str(RUN_DIR / "analysis"),
               })
    resp = await handler.handle(req)
    await _flush_bundle(bundle)
    if not resp.is_ok:
        raise RuntimeError(f"Analyzer failed: {resp.error}")
    _write_json(RUN_DIR / "analysis.json", resp.payload)


async def run_coder() -> None:
    from agents.coder_agent import CoderHandler, build_coder_agent
    clf      = _read_json(RUN_DIR / "classifier.json")
    analysis = _read_json(RUN_DIR / "analysis.json")
    source_dir   = str(RUN_DIR / "source")
    output_root  = RUN_DIR / "code"
    infra_root   = RUN_DIR / "infrastructure"
    attempt      = int(os.environ.get("CODER_ATTEMPT", "1"))
    prev_failures = os.environ.get("CODER_PREVIOUS_FAILURES")

    bundle = await build_coder_agent(
        RUN_ID,
        sandbox_root=output_root,
        codebase_type=clf.get("codebase_type"),
    )
    handler = CoderHandler(bundle.agent, nhi_id=bundle.nhi_id)
    req = _req("Orchestrator", "Coder", "migrate_module",
               "CodingRequest/v1", {
                   "module": MODULE_ID,
                   "language": clf.get("language", "python"),
                   "codebase_type": clf.get("codebase_type", "generic"),
                   "attempt": attempt,
                   "output_root": str(output_root),
                   "infra_root": str(infra_root),
                   "source_dir": source_dir,
                   "analysis_markdown": analysis.get("analysis_markdown", ""),
                   "previous_failures_json": prev_failures,
               })
    resp = await handler.handle(req)
    await _flush_bundle(bundle)
    if not resp.is_ok:
        raise RuntimeError(f"Coder failed: {resp.error}")
    _write_json(RUN_DIR / "code_report.json", resp.payload)


async def run_tester() -> None:
    from agents.tester_agent import TesterHandler, build_tester_agent
    output_root  = RUN_DIR / "code"
    attempt      = int(os.environ.get("TESTER_ATTEMPT", "1"))
    prev_failures = os.environ.get("TESTER_PREVIOUS_FAILURES")

    bundle = await build_tester_agent(RUN_ID, sandbox_root=output_root)
    handler = TesterHandler(bundle.agent, nhi_id=bundle.nhi_id)
    req = _req("Orchestrator", "Tester", "evaluate_module",
               "TestRequest/v1", {
                   "module": MODULE_ID,
                   "attempt": attempt,
                   "migrated_source_dir": str(output_root),
                   "test_dir": str(output_root / "tests"),
                   "output_dir": str(output_root / "eval"),
                   "previous_failures_json": prev_failures,
               })
    resp = await handler.handle(req)
    await _flush_bundle(bundle)
    if not resp.is_ok:
        raise RuntimeError(f"Tester failed: {resp.error}")
    _write_json(RUN_DIR / "test_report.json", resp.payload)


async def run_reviewer() -> None:
    from agents.reviewer_agent import ReviewerHandler, build_reviewer_agent
    analysis     = _read_json(RUN_DIR / "analysis.json")
    test_report  = _read_json(RUN_DIR / "test_report.json") if (RUN_DIR / "test_report.json").exists() else {}
    output_root  = RUN_DIR / "code"

    bundle = await build_reviewer_agent(RUN_ID)
    handler = ReviewerHandler(bundle.agent, nhi_id=bundle.nhi_id)
    req = _req("Orchestrator", "Reviewer", "review_module",
               "ReviewRequest/v1", {
                   "module": MODULE_ID,
                   "language": analysis.get("language", "python"),
                   "migrated_source_dir": str(output_root),
                   "analysis_markdown": analysis.get("analysis_markdown", ""),
                   "test_results_markdown": test_report.get("test_results_markdown", ""),
               })
    resp = await handler.handle(req)
    await _flush_bundle(bundle)
    if not resp.is_ok:
        raise RuntimeError(f"Reviewer failed: {resp.error}")
    _write_json(RUN_DIR / "review.json", resp.payload)


async def run_security_reviewer() -> None:
    from agents.security_reviewer_agent import SecurityReviewerHandler, build_security_reviewer_agent
    analysis    = _read_json(RUN_DIR / "analysis.json")
    output_root = RUN_DIR / "code"

    bundle = await build_security_reviewer_agent(RUN_ID)
    handler = SecurityReviewerHandler(bundle.agent, nhi_id=bundle.nhi_id)
    req = _req("Orchestrator", "SecurityReviewer", "security_review_module",
               "SecurityReviewRequest/v1", {
                   "module": MODULE_ID,
                   "language": analysis.get("language", "python"),
                   "migrated_source_dir": str(output_root),
               })
    resp = await handler.handle(req)
    await _flush_bundle(bundle)
    _write_json(RUN_DIR / "security_report.json", resp.payload)
    if resp.is_ok and resp.payload.get("recommendation") == "BLOCKED":
        logger.error("SecurityReviewer BLOCKED the migration")
        sys.exit(2)


# ── Dispatch ──────────────────────────────────────────────────────────────────

_HANDLERS: dict[str, object] = {
    "Classifier":       run_classifier,
    "Analyzer":         run_analyzer,
    "Coder":            run_coder,
    "Tester":           run_tester,
    "Reviewer":         run_reviewer,
    "SecurityReviewer": run_security_reviewer,
}


async def main() -> None:
    handler_fn = _HANDLERS.get(AGENT_TYPE)
    if handler_fn is None:
        logger.error("Unknown AGENT_TYPE=%r. Valid: %s", AGENT_TYPE, list(_HANDLERS))
        sys.exit(1)

    logger.info("Starting %s | run_id=%s module=%s", AGENT_TYPE, RUN_ID, MODULE_ID)
    t0 = time.perf_counter()
    await handler_fn()
    elapsed = time.perf_counter() - t0
    logger.info("%s completed in %.1fs", AGENT_TYPE, elapsed)


if __name__ == "__main__":
    asyncio.run(main())
