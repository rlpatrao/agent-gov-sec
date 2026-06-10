"""
tests/test_nhi_registry.py — the agnostic NHI registry (always runs).

core.nhi_registry holds the agent-type → client-id mapping and the AgentIdentity
data type. It imports no cloud SDK; credential resolution is delegated to the
provider's IdentityProvider. Here, with no Azure SDK installed, get_credential()
degrades to None rather than raising.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_registry():
    # The client-id map is read from env at import; reload after setting env.
    import core.nhi_registry as nhi
    return importlib.reload(nhi)


def test_unregistered_agent_raises():
    from core.nhi_registry import NHIRegistry
    with pytest.raises(ValueError, match="No NHI registered"):
        NHIRegistry.get("NoSuchAgent")


def test_empty_client_id_is_treated_as_unregistered(monkeypatch):
    # Default map entries are "" when the env var is unset → get() should raise.
    monkeypatch.delenv("NHI_CLIENT_ID_ANALYZER", raising=False)
    nhi = _reload_registry()
    with pytest.raises(ValueError):
        nhi.NHIRegistry.get("Analyzer")


def test_registered_agent_resolves(monkeypatch):
    monkeypatch.setenv("NHI_CLIENT_ID_ANALYZER", "11111111-2222-3333-4444-555555555555")
    nhi = _reload_registry()
    ident = nhi.NHIRegistry.get("Analyzer")
    assert ident.agent_type == "Analyzer"
    assert ident.client_id == "11111111-2222-3333-4444-555555555555"
    assert str(ident) == "Analyzer/11111111-2222-3333-4444-555555555555"


def test_env_extensibility_registers_unknown_agent(monkeypatch):
    # An agent type NOT in the static map resolves purely from its env var —
    # so a demo payload (FinOps/Auditor/Rogue) registers without editing core.
    monkeypatch.setenv("NHI_CLIENT_ID_FINOPS", "cid-finops-xyz")
    nhi = _reload_registry()
    ident = nhi.NHIRegistry.get("FinOps")
    assert ident.agent_type == "FinOps"
    assert ident.client_id == "cid-finops-xyz"


def test_unknown_agent_without_env_still_raises(monkeypatch):
    monkeypatch.delenv("NHI_CLIENT_ID_GHOST", raising=False)
    nhi = _reload_registry()
    with pytest.raises(ValueError, match="No NHI registered"):
        nhi.NHIRegistry.get("Ghost")


def test_get_credential_degrades_to_none_without_cloud_sdk(monkeypatch):
    # Simulate the Azure SDK being unavailable (it may be installed in this venv):
    # setting sys.modules["azure.identity"] = None makes the lazy
    # `from azure.identity import ...` raise ImportError, so AzureIdentityProvider
    # returns None and AgentIdentity.get_credential() degrades gracefully (never raises).
    import sys
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    monkeypatch.setenv("NHI_CLIENT_ID_ANALYZER", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("CLOUD_PROVIDER", "azure")
    nhi = _reload_registry()
    ident = nhi.NHIRegistry.get("Analyzer")
    assert ident.get_credential() is None
