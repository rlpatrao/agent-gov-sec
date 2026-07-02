"""
cloud_adapters.azure.orchestrator — Azure job orchestration (per-agent batch runs).

The Azure counterpart of ``cloud_adapters/aws/orchestrator.py`` (AWS Batch): start
each agent as a **Container Apps Job** execution under its own User-Assigned
Managed Identity, with a per-run environment override. Artifacts flow through the
Azure Files share mounted at ``/data`` (see ``infra/aca_jobs.bicep``).

This is the WS1/WS5 reference orchestrator — the Azure SDK is lazy/guarded so
importing the module needs no ``azure-mgmt-*`` install; ``submit_agent_job``
raises a clear error if the SDK is absent rather than failing at import.
Single-agent demo runs don't need it (the agent runs in-process); it exists for
the multi-agent / fan-out deployment shape described in
docs/azure/deployment-topology.html.

Env:
  AZURE_SUBSCRIPTION_ID   — target subscription
  AZURE_RESOURCE_GROUP    — resource group holding the Container Apps jobs
  AZURE_ACA_JOB_PREFIX    — job-name prefix (default "galaxy-"); job = "<prefix><agent>-job"
  AZURE_REGION            — region (informational; default eastus)
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
    resource_group: Optional[str] = None,
    subscription_id: Optional[str] = None,
    job_name: Optional[str] = None,
) -> dict[str, Any]:
    """Start one agent as a Container Apps Job execution. Returns a dict with the
    job name and the started execution name.

    Raises RuntimeError if the Azure SDK is unavailable or required config is missing.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.appcontainers import ContainerAppsAPIClient
        from azure.mgmt.appcontainers.models import (
            JobExecutionTemplate,
            JobExecutionContainer,
            EnvironmentVar,
        )
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "azure-mgmt-appcontainers not installed — "
            "`pip install azure-mgmt-appcontainers azure-identity` to use the Azure orchestrator"
        ) from e

    subscription_id = subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID")
    resource_group = resource_group or os.environ.get("AZURE_RESOURCE_GROUP")
    if not subscription_id or not resource_group:
        raise RuntimeError(
            "AZURE_SUBSCRIPTION_ID and AZURE_RESOURCE_GROUP must be set "
            "(or passed) to start an agent job."
        )

    prefix = os.environ.get("AZURE_ACA_JOB_PREFIX", "galaxy-")
    job_name = job_name or f"{prefix}{agent_type.lower()}-job"

    client = ContainerAppsAPIClient(DefaultAzureCredential(), subscription_id)
    template = JobExecutionTemplate(
        containers=[
            JobExecutionContainer(
                name="agent",
                env=[
                    EnvironmentVar(name="AGENT_TYPE", value=agent_type),
                    EnvironmentVar(name="GALAXY_RUN_ID", value=run_id),
                    EnvironmentVar(name="GALAXY_MODULE_ID", value=module_id),
                ],
            )
        ]
    )
    poller = client.jobs.begin_start(resource_group, job_name, template=template)
    execution = poller.result()
    exec_name = getattr(execution, "name", None)
    logger.info(
        "azure_orchestrator.started",
        extra={"agent_type": agent_type, "job": job_name, "execution": exec_name},
    )
    return {"job": job_name, "execution": exec_name}
