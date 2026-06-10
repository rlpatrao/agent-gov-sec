"""
tests/test_provider_factory.py — the WS1 cloud-adapter seam (agnostic).

Verifies provider selection by CLOUD_PROVIDER, the azure default, lazy import,
and that the aws/gcp skeletons resolve but raise NotImplementedError from every
accessor (locking the WS5/WS6 contract). No cloud SDK / MAF required.
"""

from __future__ import annotations

import pytest

from core import provider_factory
from core.provider_factory import available_providers, get_provider


def test_lists_all_three_providers():
    assert set(available_providers()) == {"azure", "aws", "gcp"}


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


@pytest.mark.parametrize("cloud", ["aws", "gcp"])
def test_skeleton_providers_resolve_but_not_implemented(cloud):
    p = get_provider(cloud)
    assert p.name == cloud
    for accessor in ("identity_provider", "secret_provider", "trace_exporter_factory", "llm_gateway"):
        with pytest.raises(NotImplementedError):
            getattr(p, accessor)()
    # The framework axis is intentionally absent (AWS/GCP use their own, not MAF).
    assert p.runtime_adapter() is None


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
