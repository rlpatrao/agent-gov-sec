"""
tests/test_policy_store.py — the centralized policy store.

The registry is one versioned object the governing team writes and every
enforcement tier reads. These tests pin the loading contract of
``governance.shared.policy_registry.resolve_registry``: the source precedence,
the TTL cache, the stale-serve behaviour when a refresh fails, and the
fail-closed paths (no source configured, and a first load that fails). They also
cover the ``s3://`` URI parsing and the publish side
(``governance.policy_export.publish_registry``).

No AWS is reached: the S3 client is replaced with a stub, or a fetcher is
injected directly.
"""

from __future__ import annotations

import json

import pytest

from governance.shared.policy_registry import (
    RegistryUnavailable,
    fetch_registry_uri,
    parse_s3_uri,
    policy_for,
    reset_registry_cache,
    resolve_registry,
)


def _registry(*agent_types) -> dict:
    return {"version": "1.0", "default": "deny",
            "agents": {t: {"agent_type": t} for t in agent_types}}


def _json(*agent_types) -> str:
    return json.dumps(_registry(*agent_types))


class _Clock:
    """A fake monotonic clock; advance it to cross the TTL deterministically."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_registry_cache()
    yield
    reset_registry_cache()


# ── Source precedence ────────────────────────────────────────────────────────

class TestPrecedence:
    def test_inline_json_wins_over_uri_and_path(self, tmp_path):
        path = tmp_path / "agent-controls.json"
        path.write_text(_json("FromFile"), encoding="utf-8")
        env = {"GOV_POLICY_REGISTRY": _json("FromInline"),
               "GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json",
               "GOV_POLICY_REGISTRY_PATH": str(path)}
        chosen: list[str] = []

        reg = resolve_registry(env=env, fetcher=_recording_fetcher(chosen))

        assert chosen == ["inline"], "the S3 store must not be read when JSON is supplied inline"
        assert sorted(reg["agents"]) == ["FromInline"]

    def test_uri_wins_over_path(self, tmp_path):
        path = tmp_path / "agent-controls.json"
        path.write_text(_json("FromFile"), encoding="utf-8")
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json",
               "GOV_POLICY_REGISTRY_PATH": str(path)}

        reg = resolve_registry(env=env, fetcher=lambda kind, value: (_registry("FromUri"), "v1"))

        assert sorted(reg["agents"]) == ["FromUri"]

    def test_path_is_the_last_resort(self, tmp_path):
        path = tmp_path / "agent-controls.json"
        path.write_text(_json("FromFile"), encoding="utf-8")

        reg = resolve_registry(env={"GOV_POLICY_REGISTRY_PATH": str(path)})

        assert policy_for(reg, "FromFile") == {"agent_type": "FromFile"}

    def test_blank_source_is_skipped(self, tmp_path):
        path = tmp_path / "agent-controls.json"
        path.write_text(_json("FromFile"), encoding="utf-8")
        env = {"GOV_POLICY_REGISTRY": "   ", "GOV_POLICY_REGISTRY_PATH": str(path)}

        assert sorted(resolve_registry(env=env)["agents"]) == ["FromFile"]


def _recording_fetcher(chosen: list):
    """Record which source kind the resolver selected, then load it for real."""
    from governance.shared.policy_registry import _fetch_source

    def fetcher(kind, value):
        chosen.append(kind)
        return _fetch_source(kind, value)
    return fetcher


# ── TTL cache ────────────────────────────────────────────────────────────────

class TestTtlCache:
    def _counting_fetcher(self, calls):
        def fetcher(kind, value):
            calls.append(value)
            return _registry(f"Load{len(calls)}"), f"v{len(calls)}"
        return fetcher

    def test_inside_ttl_the_cached_copy_is_served(self):
        calls: list[str] = []
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json",
               "GOV_POLICY_REGISTRY_TTL_SECONDS": "300"}
        clock = _Clock()

        first = resolve_registry(env=env, fetcher=self._counting_fetcher(calls), clock=clock)
        clock.advance(299)
        second = resolve_registry(env=env, fetcher=self._counting_fetcher(calls), clock=clock)

        assert len(calls) == 1
        assert first == second == _registry("Load1")

    def test_past_the_ttl_the_source_is_re_read(self):
        calls: list[str] = []
        fetcher = self._counting_fetcher(calls)
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json",
               "GOV_POLICY_REGISTRY_TTL_SECONDS": "300"}
        clock = _Clock()

        resolve_registry(env=env, fetcher=fetcher, clock=clock)
        clock.advance(301)
        refreshed = resolve_registry(env=env, fetcher=fetcher, clock=clock)

        assert len(calls) == 2
        assert refreshed == _registry("Load2")

    def test_default_ttl_is_300_seconds(self):
        calls: list[str] = []
        fetcher = self._counting_fetcher(calls)
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json"}
        clock = _Clock()

        resolve_registry(env=env, fetcher=fetcher, clock=clock)
        clock.advance(299)
        resolve_registry(env=env, fetcher=fetcher, clock=clock)
        assert len(calls) == 1

        clock.advance(2)
        resolve_registry(env=env, fetcher=fetcher, clock=clock)
        assert len(calls) == 2

    def test_a_changed_uri_is_not_served_from_the_previous_cache(self):
        calls: list[str] = []
        fetcher = self._counting_fetcher(calls)
        clock = _Clock()

        resolve_registry(env={"GOV_POLICY_REGISTRY_URI": "s3://bucket/a.json"},
                         fetcher=fetcher, clock=clock)
        resolve_registry(env={"GOV_POLICY_REGISTRY_URI": "s3://bucket/b.json"},
                         fetcher=fetcher, clock=clock)

        assert calls == ["s3://bucket/a.json", "s3://bucket/b.json"]

    def test_edited_inline_json_is_picked_up_within_the_ttl(self):
        clock = _Clock()
        first = resolve_registry(env={"GOV_POLICY_REGISTRY": _json("FinOps")}, clock=clock)
        second = resolve_registry(env={"GOV_POLICY_REGISTRY": _json("Auditor")}, clock=clock)

        assert sorted(first["agents"]) == ["FinOps"]
        assert sorted(second["agents"]) == ["Auditor"]


# ── Refresh failure: serve the last good copy, loudly ────────────────────────

class TestStaleServe:
    def test_refresh_failure_serves_the_last_good_copy_with_a_warning(self, caplog):
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json",
               "GOV_POLICY_REGISTRY_TTL_SECONDS": "60"}
        clock = _Clock()
        resolve_registry(env=env, clock=clock,
                         fetcher=lambda kind, value: (_registry("FinOps"), "v7"))

        clock.advance(600)
        with caplog.at_level("WARNING", logger="governance.shared.policy_registry"):
            served = resolve_registry(
                env=env, clock=clock,
                fetcher=_raiser(RuntimeError("s3 unavailable")))

        assert served == _registry("FinOps"), "must not degrade to an empty registry"
        message = caplog.text
        assert "policy_registry.refresh_failed" in message
        assert "serving_cached_age_seconds=600.0" in message
        assert "version_id=v7" in message, "the operator needs the version being served"

    def test_a_recovered_source_replaces_the_stale_copy(self):
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json",
               "GOV_POLICY_REGISTRY_TTL_SECONDS": "60"}
        clock = _Clock()
        resolve_registry(env=env, clock=clock,
                         fetcher=lambda kind, value: (_registry("FinOps"), "v7"))

        clock.advance(600)
        resolve_registry(env=env, clock=clock, fetcher=_raiser(RuntimeError("s3 unavailable")))
        recovered = resolve_registry(env=env, clock=clock,
                                     fetcher=lambda kind, value: (_registry("FinOps", "Auditor"), "v8"))

        assert sorted(recovered["agents"]) == ["Auditor", "FinOps"]


def _raiser(exc):
    def fetcher(kind, value):
        raise exc
    return fetcher


# ── Fail closed ──────────────────────────────────────────────────────────────

class TestFailClosed:
    def test_no_source_configured_raises(self):
        with pytest.raises(RegistryUnavailable) as e:
            resolve_registry(env={})
        assert "no policy registry configured" in str(e.value)

    def test_first_load_failure_raises_rather_than_returning_empty(self):
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json"}
        with pytest.raises(RegistryUnavailable):
            resolve_registry(env=env, fetcher=_raiser(RuntimeError("access denied")))

    def test_unparseable_document_raises(self, tmp_path):
        path = tmp_path / "agent-controls.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(RegistryUnavailable):
            resolve_registry(env={"GOV_POLICY_REGISTRY_PATH": str(path)})

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(RegistryUnavailable):
            resolve_registry(env={"GOV_POLICY_REGISTRY_PATH": str(tmp_path / "absent.json")})

    def test_a_deleted_file_still_serves_the_last_good_copy(self, tmp_path):
        path = tmp_path / "agent-controls.json"
        path.write_text(_json("FinOps"), encoding="utf-8")
        env = {"GOV_POLICY_REGISTRY_PATH": str(path)}
        clock = _Clock()
        resolve_registry(env=env, clock=clock)

        path.unlink()
        clock.advance(600)

        assert sorted(resolve_registry(env=env, clock=clock)["agents"]) == ["FinOps"]

    def test_a_copy_from_a_different_store_is_not_substituted(self):
        clock = _Clock()
        resolve_registry(env={"GOV_POLICY_REGISTRY_URI": "s3://store-a/agent-controls.json"},
                         fetcher=lambda kind, value: (_registry("FinOps"), "v1"), clock=clock)

        with pytest.raises(RegistryUnavailable):
            resolve_registry(env={"GOV_POLICY_REGISTRY_URI": "s3://store-b/agent-controls.json"},
                             fetcher=_raiser(RuntimeError("access denied")), clock=clock)

    def test_an_empty_registry_denies_every_identity(self):
        assert policy_for({}, "FinOps") is None

    def test_bad_ttl_falls_back_to_the_default(self):
        calls: list[str] = []
        env = {"GOV_POLICY_REGISTRY_URI": "s3://bucket/agent-controls.json",
               "GOV_POLICY_REGISTRY_TTL_SECONDS": "soon"}
        clock = _Clock()

        def fetcher(kind, value):
            calls.append(value)
            return _registry("FinOps"), "v1"

        resolve_registry(env=env, fetcher=fetcher, clock=clock)
        clock.advance(100)
        resolve_registry(env=env, fetcher=fetcher, clock=clock)

        assert len(calls) == 1, "an unparseable TTL must not disable caching"


# ── s3:// URIs ───────────────────────────────────────────────────────────────

class _StubS3:
    """Minimal stand-in for the boto3 S3 client."""

    def __init__(self, body: str, version_id: str | None = "v3"):
        self.body, self.version_id, self.calls = body, version_id, []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        obj = {"Body": _Body(self.body.encode("utf-8"))}
        if self.version_id is not None:
            obj["VersionId"] = self.version_id
        return obj

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"VersionId": self.version_id} if self.version_id is not None else {}


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class TestS3Uri:
    @pytest.mark.parametrize("uri,expected", [
        ("s3://bucket/agent-controls.json", ("bucket", "agent-controls.json")),
        ("s3://bucket/nested/prefix/agent-controls.json",
         ("bucket", "nested/prefix/agent-controls.json")),
        ("S3://bucket/key.json", ("bucket", "key.json")),
    ])
    def test_parses_bucket_and_key(self, uri, expected):
        assert parse_s3_uri(uri) == expected

    @pytest.mark.parametrize("uri", [
        "", "bucket/key.json", "https://example.com/key.json", "s3://bucket", "s3://bucket/",
        "s3:///key.json",
    ])
    def test_rejects_anything_else(self, uri):
        with pytest.raises(ValueError):
            parse_s3_uri(uri)

    def test_fetch_reads_the_object_and_returns_its_version(self, monkeypatch):
        stub = _StubS3(_json("FinOps"), version_id="v42")
        monkeypatch.setattr("governance.shared.policy_registry._s3_client", lambda: stub)

        registry, version_id = fetch_registry_uri("s3://policy-store/agent-controls.json")

        assert version_id == "v42"
        assert sorted(registry["agents"]) == ["FinOps"]
        assert stub.calls == [{"Bucket": "policy-store", "Key": "agent-controls.json"}]

    def test_unversioned_bucket_yields_no_version_id(self, monkeypatch):
        stub = _StubS3(_json("FinOps"), version_id=None)
        monkeypatch.setattr("governance.shared.policy_registry._s3_client", lambda: stub)

        _, version_id = fetch_registry_uri("s3://policy-store/agent-controls.json")

        assert version_id is None

    def test_resolve_uses_the_s3_source_end_to_end(self, monkeypatch):
        stub = _StubS3(_json("FinOps", "Auditor"))
        monkeypatch.setattr("governance.shared.policy_registry._s3_client", lambda: stub)

        reg = resolve_registry(env={"GOV_POLICY_REGISTRY_URI": "s3://policy-store/agent-controls.json"})

        assert sorted(reg["agents"]) == ["Auditor", "FinOps"]


# ── Publishing ───────────────────────────────────────────────────────────────

class TestPublish:
    def test_uploads_the_exact_bytes_and_reports_version_and_digest(self):
        from hashlib import sha256

        from governance.policy_export import publish_registry

        stub = _StubS3("", version_id="v9")
        body = _json("FinOps") + "\n"

        result = publish_registry("s3://policy-store/agent-controls.json", body, client=stub)

        assert result["version_id"] == "v9"
        assert result["digest"] == "sha256:" + sha256(body.encode("utf-8")).hexdigest()
        assert result["bucket"] == "policy-store"
        assert result["key"] == "agent-controls.json"
        call = stub.calls[0]
        assert call["Body"] == body.encode("utf-8")
        assert call["ServerSideEncryption"] == "AES256"
        assert call["ContentType"] == "application/json"

    def test_published_document_round_trips_through_the_resolver(self, monkeypatch):
        from governance.policy_export import publish_registry

        body = _json("FinOps") + "\n"
        stub = _StubS3(body, version_id="v9")
        publish_registry("s3://policy-store/agent-controls.json", body, client=stub)

        monkeypatch.setattr("governance.shared.policy_registry._s3_client", lambda: stub)
        reg = resolve_registry(env={"GOV_POLICY_REGISTRY_URI": "s3://policy-store/agent-controls.json"})

        assert sorted(reg["agents"]) == ["FinOps"]

    def test_unversioned_bucket_reports_no_version(self):
        from governance.policy_export import publish_registry

        stub = _StubS3("", version_id=None)
        result = publish_registry("s3://policy-store/agent-controls.json", _json("FinOps"), client=stub)

        assert result["version_id"] is None

    def test_rejects_a_non_s3_destination(self):
        from governance.policy_export import publish_registry

        with pytest.raises(ValueError):
            publish_registry("https://example.com/registry.json", _json("FinOps"), client=_StubS3(""))


# ── Consumer wiring ──────────────────────────────────────────────────────────

class TestConsumers:
    def test_authority_serves_the_store_and_falls_back_to_empty(self, monkeypatch):
        from governance.remote import server

        monkeypatch.delenv("GOV_POLICY_REGISTRY", raising=False)
        monkeypatch.delenv("GOV_POLICY_REGISTRY_PATH", raising=False)
        monkeypatch.setenv("GOV_POLICY_REGISTRY_URI", "s3://policy-store/agent-controls.json")
        monkeypatch.setattr("governance.shared.policy_registry._s3_client",
                            lambda: _StubS3(_json("FinOps")))
        assert sorted(server._registry()["agents"]) == ["FinOps"]

        reset_registry_cache()
        monkeypatch.delenv("GOV_POLICY_REGISTRY_URI")
        # Nothing configured: an empty registry, which denies every identity —
        # never an exception escaping into the request path.
        assert server._registry() == {}
