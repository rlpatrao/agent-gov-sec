"""
scripts/run_pipeline_aca.py

Orchestrates the migration pipeline using Azure Container App Jobs.
Each agent runs in its own job (its own container, its own Managed Identity).
Artifacts flow through the shared Azure Files mount (galaxyscannersa/galaxy-runs).

Usage:
    python scripts/run_pipeline_aca.py \
        --source-dir legacy/aws_legacy \
        --run-id run-$(date +%Y%m%d-%H%M%S) \
        --module-id aws_legacy

Prerequisites:
    az login
    Bicep deployed: az deployment group create --template-file infra/aca_jobs.bicep ...

Pipeline order:
    Classifier → Analyzer → Coder (up to 3 attempts) → Tester → Reviewer → SecurityReviewer
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("galaxy.aca-orchestrator")

# ── Config ────────────────────────────────────────────────────────────────────

RG               = os.environ.get("AZURE_RESOURCE_GROUP", "galaxyscanner-rg")
SUBSCRIPTION_ID  = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
STORAGE_ACCT     = "galaxyscannersa"
SHARE_NAME       = "galaxy-runs"
JOB_PREFIX       = "galaxy"
ACR_SERVER       = "galaxyscannercrd63cdd.azurecr.io"
DEFAULT_IMAGE_TAG = "0.3.1"
MAX_CODER_ATTEMPTS = 3
POLL_INTERVAL_SEC  = 15
JOB_TIMEOUT_SEC    = 3600  # 1 hour max per job

# Agents used by the migration pipeline (in dependency order)
PIPELINE_AGENTS = ["classifier", "analyzer", "coder", "tester", "reviewer", "securityreviewer"]


# ── Azure CLI helpers ─────────────────────────────────────────────────────────

def _az(*args: str, check: bool = True) -> dict | str:
    cmd = ["az"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"az command failed: {' '.join(cmd)}\n{result.stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def _start_job(agent_name: str, run_id: str, module_id: str,
               extra_env: dict[str, str] | None = None,
               image_tag: str = DEFAULT_IMAGE_TAG) -> str:
    """Update job env vars then trigger a Container App Job execution."""
    job_name = f"{JOB_PREFIX}-{agent_name}-job"

    # Build the env-var update: set run-specific vars while keeping AGENT_TYPE/NHI_CLIENT_ID
    set_env_vars = [f"RUN_ID={run_id}", f"MODULE_ID={module_id}"]
    if extra_env:
        set_env_vars += [f"{k}={v}" for k, v in extra_env.items()]

    # Update the job definition to inject run-specific env vars into the template
    _az(
        "containerapp", "job", "update",
        "--name", job_name,
        "--resource-group", RG,
        "--set-env-vars", *set_env_vars,
    )

    # Start without env-var overrides so the full job template (volumes, AGENT_TYPE, MI) applies
    result = _az(
        "containerapp", "job", "start",
        "--name", job_name,
        "--resource-group", RG,
    )
    execution_name = result.get("name", result) if isinstance(result, dict) else result
    logger.info("Started %s — execution: %s", agent_name, execution_name)
    return execution_name


def _wait_for_job(agent_name: str, execution_name: str) -> str:
    """Poll until the job execution reaches a terminal state. Returns 'Succeeded' or 'Failed'."""
    job_name = f"{JOB_PREFIX}-{agent_name}-job"
    deadline = time.time() + JOB_TIMEOUT_SEC
    while time.time() < deadline:
        status_result = _az(
            "containerapp", "job", "execution", "show",
            "--name", job_name,
            "--resource-group", RG,
            "--job-execution-name", execution_name,
            "--query", "properties.status",
            "-o", "tsv",
            check=False,
        )
        status = status_result.strip() if isinstance(status_result, str) else "Unknown"
        logger.info("  %s [%s]: %s", agent_name, execution_name, status)
        if status in ("Succeeded", "Failed", "Stopped"):
            return status
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"{agent_name} job did not complete within {JOB_TIMEOUT_SEC}s")


def _run_agent(agent_name: str, run_id: str, module_id: str,
               extra_env: dict[str, str] | None = None,
               image_tag: str = DEFAULT_IMAGE_TAG) -> None:
    """Start a job, wait for it to finish, raise on failure."""
    logger.info("==> %s", agent_name.upper())
    execution = _start_job(agent_name, run_id, module_id, extra_env, image_tag=image_tag)
    status = _wait_for_job(agent_name, execution)
    if status != "Succeeded":
        raise RuntimeError(f"{agent_name} job {execution} ended with status: {status}")
    logger.info("    %s OK", agent_name)


# ── Azure Files upload / download ─────────────────────────────────────────────

def _preflight_provision_jobs(image_tag: str) -> None:
    """Delete stale portal-created shell jobs then redeploy via Bicep.

    Portal-created jobs have a container named after the job (e.g.
    'galaxy-classifier-job') with no image. Bicep adds a second container
    named 'agent' but cannot remove the stale one — ACA requires ALL
    containers to have images. Deleting first lets Bicep create clean jobs.
    """
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "provision_aca_jobs.sh"

    # ── Delete existing pipeline jobs ─────────────────────────────────────────
    logger.info("==> Preflight: deleting existing pipeline jobs for clean redeploy...")
    for agent in PIPELINE_AGENTS:
        job_name = f"{JOB_PREFIX}-{agent}-job"
        logger.info("    Deleting %s...", job_name)
        _az("containerapp", "job", "delete",
            "--name", job_name, "--resource-group", RG,
            "--yes", check=False)

    # Poll until all are gone (ARM delete is async)
    logger.info("    Waiting for deletions to complete...")
    for _ in range(40):  # up to ~2 min
        remaining = []
        for agent in PIPELINE_AGENTS:
            job_name = f"{JOB_PREFIX}-{agent}-job"
            r = _az("containerapp", "job", "show",
                    "--name", job_name, "--resource-group", RG, check=False)
            if r:
                remaining.append(job_name)
        if not remaining:
            break
        logger.info("    Still deleting: %s", remaining)
        time.sleep(3)

    # ── Redeploy via Bicep ────────────────────────────────────────────────────
    logger.info("    Deploying jobs via Bicep (image tag: %s)...", image_tag)
    result = subprocess.run(
        ["bash", str(script), "--image-tag", image_tag],
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"provision_aca_jobs.sh failed (exit {result.returncode}). "
            "Check az login and that the image exists in ACR."
        )
    logger.info("    Preflight complete — all jobs configured")


def _mkdir_azure(path: str) -> None:
    """Create a directory on Azure Files, creating each parent level first."""
    parts = path.strip("/").split("/")
    for i in range(1, len(parts) + 1):
        _az(
            "storage", "directory", "create",
            "--account-name", STORAGE_ACCT,
            "--share-name", SHARE_NAME,
            "--name", "/".join(parts[:i]),
            check=False,
        )


def _upload_source(source_dir: Path, run_id: str, module_id: str) -> None:
    """Copy source repo into the Azure Files share under runs/<run_id>/source/."""
    dest_path = f"runs/{run_id}/source"
    logger.info("Uploading source → azure-files://%s/%s/%s", SHARE_NAME, STORAGE_ACCT, dest_path)
    _mkdir_azure(dest_path)
    _az(
        "storage", "file", "upload-batch",
        "--account-name", STORAGE_ACCT,
        "--destination", f"{SHARE_NAME}/{dest_path}",
        "--source", str(source_dir),
    )
    logger.info("Upload complete")


def _download_results(run_id: str, local_out: Path) -> None:
    """Download the completed run artifacts from Azure Files."""
    local_out.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading results → %s", local_out)
    _az(
        "storage", "file", "download-batch",
        "--account-name", STORAGE_ACCT,
        "--source", f"{SHARE_NAME}/runs/{run_id}",
        "--destination", str(local_out),
    )
    logger.info("Results downloaded to %s", local_out)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(source_dir: Path, run_id: str, module_id: str,
                 output_dir: Path, skip_upload: bool = False,
                 provision: bool = False,
                 image_tag: str = DEFAULT_IMAGE_TAG) -> None:
    if provision:
        _preflight_provision_jobs(image_tag)

    if not skip_upload:
        _upload_source(source_dir, run_id, module_id)

    # Phase 0: Classifier
    _run_agent("classifier", run_id, module_id, image_tag=image_tag)

    # Phase 1: Analyzer
    _run_agent("analyzer", run_id, module_id, image_tag=image_tag)

    # Phase 2+3: Coder → Tester (up to MAX_CODER_ATTEMPTS)
    tester_passed = False
    for attempt in range(1, MAX_CODER_ATTEMPTS + 1):
        logger.info("==> CODER attempt %d/%d", attempt, MAX_CODER_ATTEMPTS)
        try:
            _run_agent("coder", run_id, module_id,
                       extra_env={"CODER_ATTEMPT": str(attempt)},
                       image_tag=image_tag)
        except RuntimeError as e:
            logger.warning("Coder attempt %d failed: %s", attempt, e)
            if attempt == MAX_CODER_ATTEMPTS:
                logger.error("All Coder attempts exhausted — aborting")
                sys.exit(1)
            continue

        try:
            _run_agent("tester", run_id, module_id,
                       extra_env={"TESTER_ATTEMPT": str(attempt)},
                       image_tag=image_tag)
            tester_passed = True
            break
        except RuntimeError as e:
            logger.warning("Tester attempt %d failed: %s", attempt, e)
            if attempt == MAX_CODER_ATTEMPTS:
                logger.warning("Tests never passed — continuing to review with caveat")

    # Phase 4: Reviewer
    _run_agent("reviewer", run_id, module_id, image_tag=image_tag)

    # Phase 5: SecurityReviewer (exit 2 if BLOCKED — see run_agent_job.py)
    try:
        _run_agent("securityreviewer", run_id, module_id, image_tag=image_tag)
    except RuntimeError as e:
        if "status: Failed" in str(e):
            logger.error("SecurityReviewer BLOCKED the migration — pipeline aborted")
            _download_results(run_id, output_dir)
            sys.exit(2)
        raise

    # Download all artifacts locally
    _download_results(run_id, output_dir)
    logger.info("Pipeline complete — run_id=%s  output=%s", run_id, output_dir)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Galaxy migration pipeline via ACA jobs")
    p.add_argument("--source-dir", required=True, help="Local path to source repo")
    p.add_argument("--run-id",     required=True, help="Unique run identifier (e.g. run-20260522-1430)")
    p.add_argument("--module-id",  required=True, help="Module name (e.g. aws_lambda)")
    p.add_argument("--output-dir", default="migrated_aca", help="Local dir to download results into")
    p.add_argument("--skip-upload", action="store_true",
                   help="Skip source upload (already in Azure Files)")
    p.add_argument("--provision", action="store_true",
                   help="Delete and redeploy all pipeline jobs via Bicep before running "
                        "(required first time or after image tag change)")
    p.add_argument("--image-tag", default=DEFAULT_IMAGE_TAG,
                   help=f"Container image tag to deploy (default: {DEFAULT_IMAGE_TAG})")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_pipeline(
        source_dir=Path(args.source_dir).resolve(),
        run_id=args.run_id,
        module_id=args.module_id,
        output_dir=Path(args.output_dir) / args.run_id,
        skip_upload=args.skip_upload,
        provision=args.provision,
        image_tag=args.image_tag,
    )
