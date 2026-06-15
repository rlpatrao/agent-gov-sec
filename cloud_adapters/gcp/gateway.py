"""
cloud_adapters.gcp.gateway — GCP LLMGateway (WS6).

The single managed LLM-egress path for GCP. If ``APIGEE_VERTEX_ENDPOINT`` is
set, calls route through **Apigee** (which validates an API key and proxies to
Vertex AI, so per-agent attribution headers are enforced at the edge).
Otherwise it falls back to **direct Vertex AI** — the endpoint is the regional
``{location}-aiplatform.googleapis.com`` host and requests are authorized by the
agent's ADC / Service-Account OAuth token (no static api key changes hands).

This is the chokepoint that pairs with ``cloud_adapters/gcp/egress.yaml`` — the
allow-list declares the same Apigee / Vertex hosts as the only permitted LLM
destinations. Mirrors ``cloud_adapters/aws/gateway.AwsLLMGateway`` and
``cloud_adapters/azure/gateway.AzureLLMGateway``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from core.interfaces import EgressResolution, SecretProvider

logger = logging.getLogger(__name__)


class GcpLLMGateway:
    """LLMGateway impl: Apigee → Vertex AI, with direct-Vertex (ADC) fallback."""

    def resolve(
        self,
        *,
        agent_type: str,
        client_id: str,
        secret_provider: Optional[SecretProvider] = None,
    ) -> EgressResolution:
        apigee_endpoint = os.environ.get("APIGEE_VERTEX_ENDPOINT")
        if apigee_endpoint:
            from cloud_adapters.gcp.secrets import SecretManagerProvider

            sp = secret_provider or SecretManagerProvider(
                secret_name="galaxy-apigee-key",
                env_var_fallback="APIGEE_VERTEX_KEY",
            )
            api_key = sp.get_api_key()
            headers = {
                "x-agent-type": agent_type,
                "x-nhi-id": client_id,
                # Apigee validates this; the request is proxied to Vertex at the edge.
                "x-api-key": api_key,
            }
            return EgressResolution(
                endpoint=apigee_endpoint, mode="apigee-vertex", api_key=api_key, default_headers=headers
            )

        # Direct Vertex AI: authorized by the agent's ADC / SA OAuth token — no API key.
        location = (
            os.environ.get("VERTEX_AI_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_REGION")
            or "us-central1"
        )
        endpoint = os.environ.get(
            "VERTEX_AI_ENDPOINT", f"https://{location}-aiplatform.googleapis.com"
        )
        headers = {"x-agent-type": agent_type, "x-nhi-id": client_id}
        return EgressResolution(
            endpoint=endpoint, mode="vertex-direct", api_key="", default_headers=headers
        )
