"""
tests/test_config.py

Unit tests for agents.config — YAML → Pydantic model loading.
"""

from pathlib import Path
import textwrap

import pytest

from payload_agents.config import (
    AgentConfigModel,
    ConfigError,
    clear_config_cache,
    load_agent_config,
    load_agent_config_cached,
)


class TestLoadConfig:
    def test_load_analyzer(self):
        cfg = load_agent_config("analyzer")
        assert cfg.agent_type == "Analyzer"
        assert cfg.max_file_scan_bytes == 256_000
        assert cfg.a2a.max_files_per_dispatch == 60
        assert cfg.a2a.timeout_seconds == 120
        assert cfg.governance.enable_rogue_detection is True

    def test_load_analyzer_leaf(self):
        cfg = load_agent_config("analyzer")
        assert cfg.agent_type == "Analyzer"
        # Leaf agents legitimately have an empty allowed_recipients list —
        # the schema must permit it (not require min_length >= 1).
        assert cfg.a2a.allowed_recipients == []

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_agent_config("no_such_agent")

    def test_caching_returns_same_instance(self):
        clear_config_cache()
        a = load_agent_config_cached("analyzer")
        b = load_agent_config_cached("analyzer")
        assert a is b

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path):
        bad = tmp_path / "broken.yaml"
        bad.write_text(": this is not valid yaml\n  - [\n")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_agent_config("broken", config_dir=tmp_path)

    def test_schema_validation_surfaces_useful_errors(self, tmp_path: Path):
        # Missing required max_file_scan_bytes
        (tmp_path / "bad.yaml").write_text(textwrap.dedent("""\
            agent:
              type: Scanner
            a2a:
              allowed_recipients: []
              max_files_per_dispatch: 0
              timeout_seconds: 10
            """))
        with pytest.raises(ConfigError, match="Schema validation failed"):
            load_agent_config("bad", config_dir=tmp_path)

    def test_extra_fields_are_rejected(self, tmp_path: Path):
        # extra='forbid' means typos don't silently succeed.
        (tmp_path / "typo.yaml").write_text(textwrap.dedent("""\
            agent:
              type: Scanner
              max_file_scan_bytes: 50000
              maxFiles: 9999         # typo: camelCase; should be rejected
            a2a:
              allowed_recipients: []
              max_files_per_dispatch: 0
              timeout_seconds: 10
            """))
        with pytest.raises(ConfigError, match="Schema validation failed"):
            load_agent_config("typo", config_dir=tmp_path)


class TestConfigSchema:
    def test_schema_generation(self):
        schema = AgentConfigModel.model_json_schema()
        assert "properties" in schema
        assert "agent_type" in schema["properties"]
        assert "a2a" in schema["properties"]

    def test_file_scan_bounds_in_schema(self):
        schema = AgentConfigModel.model_json_schema()
        prop = schema["properties"]["max_file_scan_bytes"]
        assert "exclusiveMinimum" in prop
        assert "maximum" in prop


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
