"""
governance.shared.policy_registry — the NHI-keyed control authority (consumer half).

This module is dependency-free (stdlib only) so it can be vendored into a Lambda
or interceptor without pulling the agent codebase. It defines the resolved policy
shape (:class:`ControlPolicy`), the fail-closed lookups (:func:`load_registry`,
:func:`policy_for`, :func:`authorize_recipient`), the explicit deny posture
(:data:`DENY_ALL`), and :func:`resolve_registry` — the single loading contract
every enforcement tier uses to read the centralized policy store. Only the S3
source imports boto3, and it does so lazily.

The *producer* side — building the registry from the per-agent config (which
imports ``payload_agents``) — lives in ``governance.policy_export``, kept separate
precisely so this consumer half carries no agent-codebase dependency.

Fail-closed: :func:`policy_for` returns ``None`` for an unknown identity; a
chokepoint that cannot resolve a policy must deny the request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlPolicy:
    """The full resolved control posture for one agent type. Consumed by all
    enforcement tiers; the ``model_boundary`` slice is consumed by
    ``shared.enforcement.session.build_enforcement``."""

    agent_type: str
    model_boundary: dict = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    allowed_recipients: tuple[str, ...] = ()
    a2a_timeout_seconds: int = 30
    a2a_max_files: int = 0
    # Data-layer gates (enforced by the data-access proxy, which owns the
    # classification catalog; here we only carry whether the gates are on).
    data_fgac: bool = False
    data_drift: bool = False
    reasoning_guard: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# Explicit deny-all posture for fail-closed callers.
DENY_ALL = ControlPolicy(
    agent_type="<unknown>",
    model_boundary={
        "injection_enabled": True, "injection_threshold": "medium",
        "credential_enabled": True, "credential_mode": "deny",
        "budget_enabled": True, "budget_max_tokens": 1,
        "output_pii_enabled": True, "blocked_patterns": [],
    },
    allowed_tools=(), denied_tools=(), allowed_recipients=(),
    data_fgac=True, data_drift=True, reasoning_guard=True,
)


def load_registry(raw: str | dict) -> dict:
    """Parse a serialized registry (JSON string or dict). Pure stdlib."""
    return raw if isinstance(raw, dict) else json.loads(raw)


def policy_for(registry: dict, identity: Optional[str]) -> Optional[dict]:
    """Resolve the policy dict for an identity (agent type). Fail-closed:
    returns None for a missing/unknown identity, so the caller denies."""
    if not identity or not registry:
        return None
    return (registry.get("agents") or {}).get(identity)


def authorize_recipient(sender_type: str, recipient: str, registry: dict) -> tuple[bool, str]:
    """Decide whether ``sender_type`` may dispatch an A2A call to ``recipient``,
    using the sender's allow-list in the serialized ``registry``. The recipient
    may be a bare type ('Auditor') or an NHI-qualified id ('Auditor-abc'); the
    type is the first '-'-delimited segment. Fail-closed: an unknown sender is
    denied. (In-process callers that don't hold a registry should use
    ``governance.policy_export.authorize_recipient_live``.)"""
    sender_policy = policy_for(registry, sender_type)
    allowed = list((sender_policy or {}).get("allowed_recipients") or []) if sender_policy else None
    if allowed is None:
        return False, f"sender {sender_type!r} has no governance policy"
    recipient_type = (recipient or "").split("-", 1)[0]
    if recipient_type not in allowed:
        return False, f"{sender_type} may not dispatch to {recipient_type} (allowed: {allowed})"
    return True, "ok"


# ── Centralized policy store (resolver) ──────────────────────────────────────
#
# The registry is one versioned object owned by the governing team. Every
# enforcement tier resolves it through :func:`resolve_registry`, so the authority
# service and the chokepoint handlers share one loading contract instead of each
# reimplementing the environment lookup.
#
# Precedence (first source that is set wins):
#   1. ``GOV_POLICY_REGISTRY``      — inline JSON (tests, local runs, overrides)
#   2. ``GOV_POLICY_REGISTRY_URI``  — the centralized store (``s3://bucket/key``)
#   3. ``GOV_POLICY_REGISTRY_PATH`` — a file baked into the deployment image
#
# The resolved document is cached for ``GOV_POLICY_REGISTRY_TTL_SECONDS``
# (default 300). Past the TTL a refresh is attempted; if that refresh fails the
# last successfully loaded document continues to be served and a warning is
# emitted carrying the copy's age and, for the S3 source, the object VersionId it
# was read from. The resolver never substitutes an empty registry for a failed
# read, and raises :class:`RegistryUnavailable` when no document has ever loaded.

REGISTRY_TTL_DEFAULT_SECONDS = 300


class RegistryUnavailable(RuntimeError):
    """No policy registry could be resolved and none is cached. Callers must
    deny — an unresolvable registry is a governance outage, not an allow."""


@dataclass(frozen=True)
class _CachedRegistry:
    registry: dict
    source: tuple          # (kind, value) — which store the copy came from
    key: tuple             # freshness identity; changes when the document changes
    loaded_at: float
    version_id: Optional[str] = None


_cache: Optional[_CachedRegistry] = None


def reset_registry_cache() -> None:
    """Discard the cached registry. For tests and for a process that must force a
    re-read (the next :func:`resolve_registry` call reloads from the source)."""
    global _cache
    _cache = None


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` into ``(bucket, key)``. Raises ``ValueError`` for
    any other scheme or a URI missing either component."""
    scheme, sep, rest = (uri or "").partition("://")
    if not sep or scheme.lower() != "s3":
        raise ValueError(f"unsupported registry URI scheme: {uri!r} (expected s3://bucket/key)")
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3 registry URI: {uri!r} (expected s3://bucket/key)")
    return bucket, key


def _s3_client():
    """Build the S3 client. Imported lazily so this module stays stdlib-only for
    consumers that supply the registry inline or from a file."""
    import boto3
    return boto3.client("s3")


def fetch_registry_uri(uri: str) -> tuple[dict, Optional[str]]:
    """Read the registry object at ``uri`` and return ``(registry, version_id)``.
    ``version_id`` is the S3 object version when the bucket is versioned, else
    ``None``."""
    bucket, key = parse_s3_uri(uri)
    obj = _s3_client().get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return load_registry(body), obj.get("VersionId")


def _registry_ttl(env) -> float:
    raw = (env.get("GOV_POLICY_REGISTRY_TTL_SECONDS") or "").strip()
    if not raw:
        return float(REGISTRY_TTL_DEFAULT_SECONDS)
    try:
        return max(float(raw), 0.0)
    except ValueError:
        logger.warning("policy_registry.bad_ttl value=%r using_default=%s",
                       raw, REGISTRY_TTL_DEFAULT_SECONDS)
        return float(REGISTRY_TTL_DEFAULT_SECONDS)


def _registry_source(env) -> Optional[tuple[str, str]]:
    """Return ``(kind, value)`` for the highest-precedence configured source."""
    for kind, var in (("inline", "GOV_POLICY_REGISTRY"),
                      ("uri", "GOV_POLICY_REGISTRY_URI"),
                      ("path", "GOV_POLICY_REGISTRY_PATH")):
        value = (env.get(var) or "").strip()
        if value:
            return kind, value
    return None


def _source_key(kind: str, value: str) -> tuple:
    """Cache identity for a source. Inline JSON is keyed by its digest and a file
    by its path plus stat signature, so a changed document is picked up
    immediately; the URI source is keyed by the URI alone and refreshes on TTL."""
    if kind == "inline":
        return (kind, hashlib.sha256(value.encode("utf-8")).hexdigest())
    if kind == "path":
        try:
            st = os.stat(value)
            return (kind, value, st.st_mtime_ns, st.st_size)
        except OSError:
            return (kind, value)
    return (kind, value)


def _fetch_source(kind: str, value: str) -> tuple[dict, Optional[str]]:
    if kind == "inline":
        return load_registry(value), None
    if kind == "path":
        with open(value, encoding="utf-8") as fh:
            return load_registry(fh.read()), None
    return fetch_registry_uri(value)


def resolve_registry(env=None, *, fetcher=None, clock=None) -> dict:
    """Resolve the policy registry, honouring the source precedence and the TTL
    cache described above.

    ``fetcher`` (``(kind, value) -> (registry, version_id)``) and ``clock``
    (``() -> float``) are injection points for tests; production callers pass
    neither. Raises :class:`RegistryUnavailable` when no source is configured, or
    when the source cannot be read and no document has ever loaded."""
    global _cache
    env = os.environ if env is None else env
    now = (clock or time.time)()
    fetch = fetcher or _fetch_source

    source = _registry_source(env)
    if source is None:
        raise RegistryUnavailable(
            "no policy registry configured (set GOV_POLICY_REGISTRY, "
            "GOV_POLICY_REGISTRY_URI, or GOV_POLICY_REGISTRY_PATH)")

    kind, value = source
    key = _source_key(kind, value)
    cached = _cache
    fresh = cached is not None and cached.key == key and (now - cached.loaded_at) < _registry_ttl(env)
    if fresh:
        return cached.registry

    try:
        registry, version_id = fetch(kind, value)
    except Exception as e:
        # Same store, refresh failed: keep serving the last good document rather
        # than degrading to an empty (deny-everything) registry, and make the
        # staleness visible. A copy read from a *different* store is not a
        # substitute, so that case raises.
        if cached is not None and cached.source == source:
            logger.warning(
                "policy_registry.refresh_failed source=%s serving_cached_age_seconds=%.1f "
                "version_id=%s error=%s",
                kind, now - cached.loaded_at, cached.version_id or "-", str(e)[:200])
            return cached.registry
        raise RegistryUnavailable(f"policy registry unavailable from {kind} source: {e}") from e

    _cache = _CachedRegistry(registry=registry, source=source, key=key,
                             loaded_at=now, version_id=version_id)
    logger.info("policy_registry.loaded source=%s version_id=%s agent_types=%d",
                kind, version_id or "-", len((registry.get("agents") or {})))
    return registry
