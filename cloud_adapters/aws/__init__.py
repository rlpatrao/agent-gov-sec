"""
cloud_adapters.aws — AWS cloud bindings (WS5).

``PROVIDER`` implements ``core.interfaces.CloudProvider`` so ``CLOUD_PROVIDER=aws``
resolves the AWS bindings:

  identity  → per-agent IAM role via STS AssumeRole / IRSA   (identity.AwsIdentityProvider)
  secrets   → Secrets Manager + SSM Parameter Store          (secrets.SecretsManagerProvider)
  tracing   → OTel → ADOT collector → X-Ray / CloudWatch     (tracing.AwsTraceExporterFactory)
  gateway   → API Gateway → Bedrock (direct-Bedrock/SigV4)   (gateway.AwsLLMGateway)
  audit     → SHA-256 hash-chain ledger on DynamoDB          (audit.DynamoDbHashChainBackend)
  egress    → AWS endpoint allow-list                        (egress.yaml)

Every accessor lazy-imports its implementation, and each implementation
lazy-imports ``boto3``, so importing this package (or the provider factory)
needs no AWS SDK. The framework axis (``runtime_adapter``) is intentionally
``None`` — AWS would wire LangGraph / Bedrock Agents (WS5.8), not MAF.

See docs/REFACTOR_AND_GAPS_PLAN.md WS5 and docs/aws-deployment-topology.html.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

_EGRESS_YAML = Path(__file__).parent / "egress.yaml"


class AwsProvider:
    """CloudProvider impl: AWS cloud bindings."""

    name = "aws"

    def secret_provider(self, **kwargs: Any):
        from cloud_adapters.aws.secrets import SecretsManagerProvider
        return SecretsManagerProvider(**kwargs)

    def identity_provider(self):
        from cloud_adapters.aws.identity import AwsIdentityProvider
        return AwsIdentityProvider()

    def trace_exporter_factory(self):
        from cloud_adapters.aws.tracing import AwsTraceExporterFactory
        return AwsTraceExporterFactory()

    def llm_gateway(self):
        from cloud_adapters.aws.gateway import AwsLLMGateway
        return AwsLLMGateway()

    def runtime_adapter(self):
        # AWS uses its own framework adapter (LangGraph / Bedrock Agents), not MAF — WS5.8.
        return None

    async def audit_backend(self, run_id: str):
        from cloud_adapters.aws.audit import DynamoDbHashChainBackend
        return await DynamoDbHashChainBackend.create(run_id=run_id)

    def egress_config_path(self) -> Optional[Path]:
        return _EGRESS_YAML if _EGRESS_YAML.exists() else None


PROVIDER = AwsProvider()
