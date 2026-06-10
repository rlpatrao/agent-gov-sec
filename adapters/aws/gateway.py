"""
adapters.aws.gateway — AWS LLMGateway.

The single managed LLM-egress path for AWS. If ``AWS_BEDROCK_GATEWAY_ENDPOINT``
is set, calls route through **API Gateway** (which validates an API key and
proxies to Amazon Bedrock, so per-agent attribution headers are enforced at the
edge). Otherwise it falls back to **direct Bedrock** — the endpoint is the
regional ``bedrock-runtime`` host and requests are SigV4-signed by the agent's
IAM credentials (no static api key changes hands).

This is the chokepoint that pairs with ``adapters/aws/egress.yaml`` — the
allow-list declares the same API-Gateway/Bedrock hosts as the only permitted
LLM destinations. Mirrors ``adapters/azure/gateway.AzureLLMGateway``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from core.interfaces import EgressResolution, SecretProvider

logger = logging.getLogger(__name__)


class AwsLLMGateway:
    """LLMGateway impl: API Gateway → Bedrock, with direct-Bedrock (SigV4) fallback."""

    def resolve(
        self,
        *,
        agent_type: str,
        client_id: str,
        secret_provider: Optional[SecretProvider] = None,
    ) -> EgressResolution:
        gateway_endpoint = os.environ.get("AWS_BEDROCK_GATEWAY_ENDPOINT")
        if gateway_endpoint:
            from adapters.aws.secrets import SecretsManagerProvider

            sp = secret_provider or SecretsManagerProvider(
                secret_name="galaxy/bedrock-gateway-key",
                env_var_fallback="AWS_BEDROCK_GATEWAY_KEY",
            )
            api_key = sp.get_api_key()
            headers = {
                "x-agent-type": agent_type,
                "x-nhi-id": client_id,
                # API Gateway validates this; the request is proxied to Bedrock at the edge.
                "x-api-key": api_key,
            }
            return EgressResolution(
                endpoint=gateway_endpoint, mode="apigw-bedrock", api_key=api_key, default_headers=headers
            )

        # Direct Bedrock: SigV4-signed by the agent's IAM credentials — no API key.
        region = os.environ.get("AWS_REGION", "us-east-1")
        endpoint = os.environ.get(
            "AWS_BEDROCK_ENDPOINT", f"https://bedrock-runtime.{region}.amazonaws.com"
        )
        headers = {"x-agent-type": agent_type, "x-nhi-id": client_id}
        return EgressResolution(
            endpoint=endpoint, mode="bedrock-direct", api_key="", default_headers=headers
        )
