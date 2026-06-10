"""
tests/test_secrets.py — the agnostic env-var SecretProvider (always runs).

core.secrets.EnvVarSecretProvider is the cloud-neutral default behind the
SecretProvider interface; the Azure Key Vault path lives in
adapters/azure/secrets.py and is exercised by the Azure integration tier.
"""

from __future__ import annotations

import pytest

from core.secrets import EnvVarSecretProvider


def test_reads_from_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-123")
    sp = EnvVarSecretProvider(env_var_fallback="MY_KEY")
    assert sp.get_api_key() == "secret-123"


def test_caches_until_invalidated(monkeypatch):
    monkeypatch.setenv("MY_KEY", "first")
    sp = EnvVarSecretProvider(env_var_fallback="MY_KEY")
    assert sp.get_api_key() == "first"
    monkeypatch.setenv("MY_KEY", "second")
    assert sp.get_api_key() == "first"          # cached
    sp.invalidate()
    assert sp.get_api_key() == "second"          # refreshed


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    sp = EnvVarSecretProvider(env_var_fallback="ABSENT_KEY")
    with pytest.raises(EnvironmentError, match="ABSENT_KEY"):
        sp.get_api_key()


def test_satisfies_secret_provider_protocol():
    from core.interfaces import SecretProvider
    assert isinstance(EnvVarSecretProvider(env_var_fallback="X"), SecretProvider)
