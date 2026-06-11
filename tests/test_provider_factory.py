"""
tests/test_provider_factory.py — the WS1 cloud-adapter seam (agnostic).

Verifies provider selection by CLOUD_PROVIDER, the azure default, lazy import,
and that the azure/aws/gcp providers resolve every accessor (WS1/WS5/WS6 are all
implemented). No cloud SDK / MAF required.
"""

from __future__ import annotations

import pytest

from core import provider_factory
from core.provider_factory import available_providers, get_provider


def test_lists_all_providers():
    assert set(available_providers()) == {"azure", "aws", "gcp", "local"}


def test_default_is_azure(monkeypatch):
    monkeypatch.delenv("CLOUD_PROVIDER", raising=False)
    assert get_provider().name == "azure"


def test_cloud_provider_env_selects(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    assert get_provider().name == "aws"


def test_explicit_name_overrides_env(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    assert get_provider("gcp").name == "gcp"


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown CLOUD_PROVIDER"):
        get_provider("rackspace")


def test_azure_provider_resolves_accessors():
    az = get_provider("azure")
    # These return real objects without needing the Azure SDK installed.
    assert az.identity_provider() is not None
    assert az.trace_exporter_factory() is not None
    assert az.llm_gateway() is not None
    assert az.runtime_adapter() is not None
    assert az.egress_config_path() is not None and az.egress_config_path().name == "egress.yaml"


def test_gcp_provider_implemented():
    # GCP implemented in WS6: every accessor resolves (no NotImplementedError),
    # each lazy-importing its Google SDK only when actually used.
    p = get_provider("gcp")
    assert p.name == "gcp"
    assert p.identity_provider() is not None
    assert p.secret_provider() is not None
    assert p.trace_exporter_factory() is not None
    assert p.llm_gateway() is not None
    # The framework axis is intentionally absent (AWS/GCP use their own, not MAF).
    assert p.runtime_adapter() is None
    assert p.egress_config_path() is not None and p.egress_config_path().name == "egress.yaml"


def test_factory_module_imports_no_cloud_sdk():
    # Importing the factory itself must not pull a cloud SDK. Checked in a clean
    # subprocess because the surrounding test session may already have imported
    # azure.* (the lazy import only fires when a provider accessor is called).
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    code = (
        "import sys, core.provider_factory; "
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in {'azure', 'boto3', 'google'}); "
        "assert not leaked, leaked"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(repo_root)
    )
    assert result.returncode == 0, result.stderr
    assert provider_factory is not None
