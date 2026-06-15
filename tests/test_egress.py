"""
tests/test_egress.py — egress allow-list (agnostic, always runs).

The egress guard (governance/guards/egress.py) is MAF-free and resolves its
allow-list path from the provider factory. These tests load the Azure adapter's
egress.yaml both explicitly and via the factory; no cloud SDK or MAF required.
"""

from __future__ import annotations

from pathlib import Path

from governance.guards.egress import load_egress_policy

_AZURE_EGRESS = Path(__file__).parent.parent / "cloud_adapters" / "azure" / "egress.yaml"


def test_allow_list_loads_from_explicit_path():
    policy = load_egress_policy(yaml_path=_AZURE_EGRESS)
    # APIM and AOAI allowed; arbitrary host denied.
    assert policy.check_url("https://example-apim.azure-api.net/openai/v1/responses").allowed is True
    assert policy.check_url("https://example-openai.openai.azure.com/").allowed is True
    assert policy.check_url("https://example.com/").allowed is False


def test_allow_list_resolves_via_provider_factory(monkeypatch):
    # No explicit path → guard asks core.provider_factory for the azure egress file.
    monkeypatch.setenv("CLOUD_PROVIDER", "azure")
    policy = load_egress_policy()
    assert policy.check_url("https://example-openai.openai.azure.com/").allowed is True
    assert policy.check_url("https://evil.example.com/").allowed is False


def test_missing_path_is_default_deny():
    policy = load_egress_policy(yaml_path=Path("/nonexistent/egress.yaml"))
    # Default-deny when the allow-list file is absent.
    assert policy.check_url("https://example-apim.azure-api.net/").allowed is False
