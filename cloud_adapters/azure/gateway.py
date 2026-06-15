"""
cloud_adapters.azure.gateway — Azure LLMGateway.

The single managed LLM-egress path. If ``APIM_ENDPOINT`` is set, calls route
through API Management (which validates the ``Ocp-Apim-Subscription-Key`` and
injects the real Azure OpenAI key from a Key-Vault-backed named value, so the
AOAI key never leaves the gateway). Otherwise it falls back to calling Azure
OpenAI directly with the key as ``api-key``.

This is the chokepoint that pairs with ``cloud_adapters/azure/egress.yaml`` — the
allow-list declares the same APIM/AOAI hosts as the only permitted LLM
destinations. The previous inline logic lived in ``payload_agents/_base._resolve_egress``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from core.interfaces import EgressResolution, SecretProvider

logger = logging.getLogger(__name__)


class AzureLLMGateway:
    """LLMGateway impl: APIM → Azure OpenAI, with direct-AOAI fallback."""

    def resolve(
        self,
        *,
        agent_type: str,
        client_id: str,
        secret_provider: Optional[SecretProvider] = None,
    ) -> EgressResolution:
        from cloud_adapters.azure.secrets import TokenProvider

        apim_endpoint = os.environ.get("APIM_ENDPOINT")
        if apim_endpoint:
            sp = secret_provider or TokenProvider(
                secret_name="apim-subscription-key",
                env_var_fallback="APIM_SUBSCRIPTION_KEY",
            )
            api_key = sp.get_api_key()
            headers = {
                "x-agent-type": agent_type,
                "x-nhi-id": client_id,
                # APIM validates this; the real AOAI key is injected at the edge.
                "Ocp-Apim-Subscription-Key": api_key,
            }
            return EgressResolution(
                endpoint=apim_endpoint, mode="apim", api_key=api_key, default_headers=headers
            )

        sp = secret_provider or TokenProvider(
            secret_name="azure-openai-key",
            env_var_fallback="AZURE_OPENAI_KEY",
        )
        api_key = sp.get_api_key()
        headers = {"x-agent-type": agent_type, "x-nhi-id": client_id}
        return EgressResolution(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            mode="aoai-direct",
            api_key=api_key,
            default_headers=headers,
        )
