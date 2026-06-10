"""
tests/test_gateway.py — the Azure LLMGateway egress chokepoint (agnostic test).

AzureLLMGateway.resolve() is the APIM-vs-direct-AOAI selection that used to be
inlined in _base._resolve_egress. We drive it with a fake SecretProvider so no
Azure SDK is constructed — the test pins the endpoint/mode/header contract that
payload_agents/_base.py depends on.
"""

from __future__ import annotations

from adapters.azure.gateway import AzureLLMGateway


class _FakeSecret:
    def __init__(self, key: str = "fake-key-abc") -> None:
        self._key = key

    def get_api_key(self) -> str:
        return self._key

    def invalidate(self) -> None:
        pass


def test_apim_mode_when_endpoint_set(monkeypatch):
    monkeypatch.setenv("APIM_ENDPOINT", "https://example-apim.azure-api.net")
    res = AzureLLMGateway().resolve(
        agent_type="Analyzer", client_id="cid-123", secret_provider=_FakeSecret("sub-key"),
    )
    assert res.mode == "apim"
    assert res.endpoint == "https://example-apim.azure-api.net"
    assert res.api_key == "sub-key"
    assert res.default_headers["Ocp-Apim-Subscription-Key"] == "sub-key"
    assert res.default_headers["x-agent-type"] == "Analyzer"
    assert res.default_headers["x-nhi-id"] == "cid-123"


def test_direct_aoai_mode_when_no_apim(monkeypatch):
    monkeypatch.delenv("APIM_ENDPOINT", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example-openai.openai.azure.com/")
    res = AzureLLMGateway().resolve(
        agent_type="Analyzer", client_id="cid-123", secret_provider=_FakeSecret("aoai-key"),
    )
    assert res.mode == "aoai-direct"
    assert res.endpoint == "https://example-openai.openai.azure.com/"
    assert res.api_key == "aoai-key"
    # Direct mode must NOT present the APIM subscription header.
    assert "Ocp-Apim-Subscription-Key" not in res.default_headers
    assert res.default_headers["x-agent-type"] == "Analyzer"
    assert res.default_headers["x-nhi-id"] == "cid-123"


def test_satisfies_llm_gateway_protocol():
    from core.interfaces import LLMGateway
    assert isinstance(AzureLLMGateway(), LLMGateway)
