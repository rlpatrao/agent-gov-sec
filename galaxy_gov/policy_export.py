"""
galaxy_gov.policy_export — build the policy registry from per-agent config.

The *producer* side of the policy registry (the consumer side is
``galaxy_gov.shared.policy_registry``). This module imports ``payload_agents``
to resolve each agent's floored posture, so it is build-time / in-process only —
it is deliberately NOT under ``galaxy_gov/shared`` so the consumer half stays
free of any agent-codebase dependency and remains vendorable into a Lambda.

`export_registry_json()` produces the artifact deployed to the out-of-process
chokepoints; `publish_registry()` writes that artifact to the centralized policy
store (the versioned S3 object the enforcement tiers read); `resolve_policy()` is
used in-process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from galaxy_gov.shared.policy_registry import ControlPolicy, authorize_recipient, parse_s3_uri

logger = logging.getLogger(__name__)

# Default location of the per-agent configs, relative to the repo root. Override
# with GOV_AGENT_CONFIG_DIR when the agent codebase lives outside this tree (the
# platform wheel does not ship payload_agents).
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "payload_agents" / "config"


def discover_agent_types(config_dir: Path | str | None = None) -> tuple[str, ...]:
    """Return every agent type the filesystem declares, sorted.

    The per-agent config directory is the single source of truth for *which
    agents exist*: one ``<slug>.yaml`` per agent, each declaring ``agent.type``.
    Deriving the list here removes the class of failure where a scaffolded agent
    is silently absent from the exported registry, the Terraform ``agent_types``
    variable, or AgentCore provisioning — each of which previously read a
    hand-maintained tuple that nothing checked against the filesystem.

    Resolution order:
      1. ``config_dir`` argument, if given.
      2. ``GOV_AGENT_CONFIG_DIR`` env — for deployments where the agent codebase
         is a separate package.
      3. ``GOV_AGENT_TYPES`` env (comma-separated) — the explicit escape hatch for
         a chokepoint that has no access to the agent configs at all.
      4. The in-tree ``payload_agents/config/``.

    Returns an empty tuple when no source resolves. Callers that provision or
    export must treat empty as "nothing to do", never as "allow everything" —
    the registry's ``default: deny`` and ``policy_for`` returning ``None`` keep
    an unknown agent denied regardless.
    """
    explicit = os.environ.get("GOV_AGENT_TYPES")
    if config_dir is None and not os.environ.get("GOV_AGENT_CONFIG_DIR") and explicit:
        return tuple(sorted({t.strip() for t in explicit.split(",") if t.strip()}))

    path = Path(config_dir or os.environ.get("GOV_AGENT_CONFIG_DIR") or _DEFAULT_CONFIG_DIR)
    if not path.is_dir():
        logger.warning("policy_export.config_dir_missing", extra={"path": str(path)})
        return ()

    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a core dependency
        logger.warning("policy_export.yaml_unavailable", extra={"path": str(path)})
        return ()

    found: set[str] = set()
    for cfg in sorted(path.glob("*.yaml")):
        try:
            raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            agent_type = ((raw.get("agent") or {}).get("type") or "").strip()
        except Exception as e:
            logger.warning("policy_export.config_unreadable",
                           extra={"path": str(cfg), "error": str(e)[:200]})
            continue
        if agent_type:
            found.add(agent_type)
        else:
            logger.warning("policy_export.config_missing_type", extra={"path": str(cfg)})
    return tuple(sorted(found))


# Agent types the platform knows about, derived from the filesystem at import
# time. Retained as a module-level tuple because provisioning and export call
# sites consume it as a constant; call `discover_agent_types()` directly to pick
# up a config added during the life of the process.
KNOWN_AGENT_TYPES = discover_agent_types()


def resolve_policy(agent_type: str) -> ControlPolicy:
    """Build the resolved (floored) control posture for ``agent_type`` from its
    per-agent config. Raises if the agent has no config — unknown agents have no
    posture and must be denied, never defaulted to permissive."""
    from payload_agents.config import load_agent_config

    cfg = load_agent_config(agent_type)          # floor already applied inside
    g = cfg.governance
    model_boundary = {
        "injection_enabled": g.enable_prompt_injection_guard,
        "injection_threshold": g.prompt_injection_block_threshold,
        "credential_enabled": g.enable_credential_redactor,
        "credential_mode": g.credential_mode,
        "budget_enabled": g.enable_context_budget,
        "budget_max_tokens": g.context_budget_tokens,
        "output_pii_enabled": True,   # output redaction is always-on at the boundary
        "blocked_patterns": list(g.blocked_patterns),
    }
    return ControlPolicy(
        agent_type=cfg.agent_type,
        model_boundary=model_boundary,
        allowed_tools=tuple(g.allowed_tools),
        denied_tools=tuple(g.denied_tools),
        allowed_recipients=tuple(cfg.a2a.allowed_recipients),
        a2a_timeout_seconds=cfg.a2a.timeout_seconds,
        a2a_max_files=cfg.a2a.max_files_per_dispatch,
        data_fgac=g.enable_data_fgac,
        data_drift=g.enable_data_drift,
        reasoning_guard=g.enable_reasoning_guard,
    )


def export_registry(agent_types: tuple[str, ...] | None = None) -> dict:
    """Resolve every known agent and return a JSON-dumpable registry keyed by
    agent type. This is the artifact deployed to each out-of-process chokepoint.

    ``agent_types`` defaults to a fresh :func:`discover_agent_types` call rather
    than the import-time tuple, so an agent added after import is exported."""
    registry: dict = {"version": "1.0", "default": "deny", "agents": {}}
    for at in agent_types if agent_types is not None else discover_agent_types():
        try:
            registry["agents"][at] = resolve_policy(at).to_dict()
        except Exception as e:
            logger.warning("policy_export.resolve_failed", extra={"agent": at, "error": str(e)})
    return registry


def export_registry_json(agent_types: tuple[str, ...] | None = None) -> str:
    return json.dumps(export_registry(agent_types), indent=2, sort_keys=True)


def publish_registry(uri: str, payload: str | bytes, *, client=None) -> dict:
    """Write the serialized registry to the centralized policy store.

    ``uri`` is an ``s3://bucket/key`` target — the bucket is expected to have
    versioning enabled, so each publication produces a new object version rather
    than overwriting the previous document. The exact bytes given are uploaded,
    so the digest reported here is the digest of the local artifact as well.

    Returns ``{"uri", "bucket", "key", "version_id", "digest"}``. ``version_id``
    is ``None`` when the bucket is not versioned, which the caller should treat
    as a misconfiguration of the store.
    """
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    digest = hashlib.sha256(body).hexdigest()
    bucket, key = parse_s3_uri(uri)

    if client is None:
        import boto3
        client = boto3.client("s3")

    response = client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        ServerSideEncryption="AES256",
        Metadata={"sha256": digest},
    )
    return {
        "uri": uri,
        "bucket": bucket,
        "key": key,
        "version_id": response.get("VersionId"),
        "digest": "sha256:" + digest,
    }


def authorize_recipient_live(sender_type: str, recipient: str) -> tuple[bool, str]:
    """In-process A2A authorization: resolve the sender's policy live and apply
    the dependency-free decision. For callers that don't hold a serialized
    registry (e.g. the in-process dispatcher)."""
    try:
        registry = {"agents": {sender_type: resolve_policy(sender_type).to_dict()}}
    except Exception:
        return False, f"sender {sender_type!r} has no governance policy"
    return authorize_recipient(sender_type, recipient, registry)


if __name__ == "__main__":
    # `python -m galaxy_gov.policy_export` prints the registry artifact to stdout,
    # used at deploy time to bake the chokepoint policy (agent-controls.json).
    print(export_registry_json())
