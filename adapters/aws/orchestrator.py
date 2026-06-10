"""
adapters.aws.orchestrator — AWS job orchestration (per-agent batch runs).

The AWS analogue of the (archived) Azure ACA-jobs orchestrator: submit each
agent as an **AWS Batch** job (or an ECS Fargate ``RunTask``) under its own IAM
role, and poll for completion. Artifacts flow through S3.

This is the WS5 reference orchestrator — boto3 is lazy/guarded so importing the
module needs no AWS SDK; ``submit_agent_job`` raises a clear error if boto3 is
absent rather than failing at import. Single-agent demo runs don't need it (the
agent runs in-process); it exists for the multi-agent / fan-out deployment shape
described in docs/aws-deployment-topology.html.

Env:
  AWS_BATCH_JOB_QUEUE       — the Batch job queue ARN/name
  AWS_BATCH_JOB_DEFINITION  — the Batch job definition (the galaxy-agent image)
  AWS_REGION                — region (default us-east-1)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def submit_agent_job(
    *,
    agent_type: str,
    run_id: str,
    module_id: str,
    job_queue: Optional[str] = None,
    job_definition: Optional[str] = None,
) -> dict[str, Any]:
    """Submit one agent as an AWS Batch job. Returns the SubmitJob response.

    Raises RuntimeError if boto3 is unavailable or required config is missing.
    """
    try:
        import boto3
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("boto3 not installed — `pip install '.[aws]'` to use the AWS orchestrator") from e

    job_queue = job_queue or os.environ.get("AWS_BATCH_JOB_QUEUE")
    job_definition = job_definition or os.environ.get("AWS_BATCH_JOB_DEFINITION")
    if not job_queue or not job_definition:
        raise RuntimeError(
            "AWS_BATCH_JOB_QUEUE and AWS_BATCH_JOB_DEFINITION must be set "
            "(or passed) to submit an agent job."
        )

    batch = boto3.client("batch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = batch.submit_job(
        jobName=f"galaxy-{agent_type}-{run_id}"[:128],
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={
            "environment": [
                {"name": "AGENT_TYPE", "value": agent_type},
                {"name": "GALAXY_RUN_ID", "value": run_id},
                {"name": "GALAXY_MODULE_ID", "value": module_id},
            ]
        },
    )
    logger.info("aws_orchestrator.submitted", extra={"agent_type": agent_type, "job_id": resp.get("jobId")})
    return resp
