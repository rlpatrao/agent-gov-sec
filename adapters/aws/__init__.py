"""
adapters.aws — AWS cloud bindings (SKELETON — WS5).

The contract is locked: ``PROVIDER`` implements ``core.interfaces.CloudProvider``
so ``CLOUD_PROVIDER=aws`` resolves cleanly, but every accessor raises
``NotImplementedError`` until WS5 fills them in:

  identity  → per-agent IAM role via IRSA / STS AssumeRole
  secrets   → Secrets Manager + SSM Parameter Store (boto3 chain)
  tracing   → OTel → X-Ray via ADOT (or CloudWatch OTLP)
  audit     → hash-chain ledger on DynamoDB / QLDB
  gateway   → API Gateway → Bedrock (or direct Bedrock + SigV4)
  egress    → AWS endpoint allow-list (Bedrock, Secrets Manager, STS, …)

See docs/REFACTOR_AND_GAPS_PLAN.md WS5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

_WS = "WS5 — adapters/aws not yet implemented"


def _todo(what: str):
    raise NotImplementedError(f"{_WS}: {what}")


class AwsProvider:
    name = "aws"

    def secret_provider(self, **kwargs: Any):
        _todo("SecretProvider (Secrets Manager + SSM)")

    def identity_provider(self):
        _todo("IdentityProvider (IAM role via IRSA / STS AssumeRole)")

    def trace_exporter_factory(self):
        _todo("TraceExporterFactory (X-Ray via ADOT)")

    def llm_gateway(self):
        _todo("LLMGateway (API Gateway → Bedrock)")

    def runtime_adapter(self):
        # AWS uses its own framework adapter (LangGraph / Bedrock Agents), not MAF.
        return None

    async def audit_backend(self, run_id: str):
        _todo("AuditBackend (DynamoDB / QLDB hash-chain ledger)")

    def egress_config_path(self) -> Optional[Path]:
        path = Path(__file__).parent / "egress.yaml"
        return path if path.exists() else None


PROVIDER = AwsProvider()
