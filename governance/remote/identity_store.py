"""
governance/remote/identity_store.py — the authority-side identity binding store.

Holds the mapping ``agent_type → cloud principal`` (on AWS, an IAM role ARN) on
the **governance** side of the trust boundary, rather than in the agent's own
environment.

Why this exists
---------------
``core.nhi_registry`` historically resolved an agent's NHI from
``NHI_CLIENT_ID_<AGENT_TYPE>`` in the agent's own process environment. That makes
the agent the asserter of its own identity: any value it sets is the value the
platform attributes its actions to. Moving the binding here — a file the agent
runtime cannot write, served by the Governance Authority under a separate
identity — makes the binding *resolved* rather than *claimed*, the same property
mechanism 3 gives the control policy.

What a binding is, and is not
-----------------------------
A binding is an **identity** statement only: "this agent type runs as this
principal." It carries no capabilities. Being enrolled does **not** make an agent
authorized: the chokepoints still resolve a ``ControlPolicy`` from the policy
registry, and ``policy_for`` returning ``None`` still denies. Enrollment and
policy approval are two independent keys and both are required — see
``governance.remote.registrar``.

Storage format (``GOV_IDENTITY_STORE``, default
``governance/configs/identity-bindings.yaml``)::

    version: "1.0"
    bindings:
      FinOps:
        principal_id: arn:aws:iam::123456789012:role/galaxy-rp-FinOps
        cloud: aws
        account: "123456789012"
        status: active
        enrolled_at: "2026-08-06T09:15:00Z"
        enrolled_by: arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Dev/alice

Failure posture: a malformed store raises on write (never clobber state we cannot
parse) and reads as "no bindings" on lookup (fail-closed — an unresolvable
identity is denied, not defaulted).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
_STATUSES = (STATUS_ACTIVE, STATUS_REVOKED)

_DEFAULT_STORE = Path(__file__).resolve().parents[1] / "configs" / "identity-bindings.yaml"

_lock = threading.Lock()


class IdentityStoreError(Exception):
    """The store file exists but cannot be parsed, or a write failed."""


def utc_now() -> str:
    """ISO-8601 UTC timestamp, second precision, ``Z``-suffixed."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class IdentityBinding:
    """One ``agent_type → principal`` binding, as recorded by the Registrar."""

    agent_type: str
    principal_id: str
    cloud: str
    account: str = ""
    status: str = STATUS_ACTIVE
    enrolled_at: str = ""
    enrolled_by: str = ""

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def to_dict(self) -> dict:
        """Serialized form, without the redundant agent_type (it is the map key)."""
        return {
            "principal_id": self.principal_id,
            "cloud": self.cloud,
            "account": self.account,
            "status": self.status,
            "enrolled_at": self.enrolled_at,
            "enrolled_by": self.enrolled_by,
        }

    @classmethod
    def from_dict(cls, agent_type: str, raw: dict) -> "IdentityBinding":
        return cls(
            agent_type=agent_type,
            principal_id=str(raw.get("principal_id") or ""),
            cloud=str(raw.get("cloud") or ""),
            account=str(raw.get("account") or ""),
            status=str(raw.get("status") or STATUS_ACTIVE),
            enrolled_at=str(raw.get("enrolled_at") or ""),
            enrolled_by=str(raw.get("enrolled_by") or ""),
        )


class IdentityStore:
    """File-backed binding store. Writes are atomic (temp file + ``os.replace``)
    and serialized under a process lock, so concurrent Registrar requests on the
    threaded server cannot interleave a read-modify-write.

    KNOWN LIMIT (I3 in docs/shared/agent-registration-plan.md): the lock is
    process-local, so this implementation is safe for a **single-replica** authority
    only. Two replicas writing the same file would lose updates. A multi-replica
    authority needs a backend with conditional writes (DynamoDB with a version
    attribute, or S3 with ``If-Match``) behind this same interface.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get("GOV_IDENTITY_STORE") or _DEFAULT_STORE)

    # ── read ─────────────────────────────────────────────────────────────────

    def load(self) -> dict[str, IdentityBinding]:
        """Parse the store. Raises :class:`IdentityStoreError` if the file exists
        but is not a valid mapping; returns ``{}`` when it does not exist yet."""
        if not self.path.exists():
            return {}
        try:
            import yaml
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise IdentityStoreError(f"cannot parse identity store {self.path}: {e}") from e
        if not isinstance(raw, dict):
            raise IdentityStoreError(f"{self.path}: top-level must be a mapping")
        bindings = raw.get("bindings") or {}
        if not isinstance(bindings, dict):
            raise IdentityStoreError(f"{self.path}: 'bindings' must be a mapping")
        out: dict[str, IdentityBinding] = {}
        for agent_type, entry in bindings.items():
            if not isinstance(entry, dict):
                raise IdentityStoreError(f"{self.path}: binding {agent_type!r} must be a mapping")
            out[str(agent_type)] = IdentityBinding.from_dict(str(agent_type), entry)
        return out

    def get(self, agent_type: str) -> Optional[IdentityBinding]:
        """Resolve one binding. Fail-closed: returns ``None`` for an unknown type,
        a revoked binding, or an unparseable store (logged, never raised, so a
        corrupt store denies rather than crashing the read path)."""
        if not agent_type:
            return None
        try:
            binding = self.load().get(agent_type)
        except IdentityStoreError as e:
            logger.error("identity_store.unreadable", extra={"error": str(e)})
            return None
        if binding is None or not binding.active:
            return None
        return binding

    # ── write ────────────────────────────────────────────────────────────────

    def put(self, binding: IdentityBinding, *, allow_rotate: bool = False) -> IdentityBinding:
        """Record ``binding``.

        Idempotent: re-recording an identical principal returns the stored
        binding untouched. Rebinding an agent type to a *different* principal is
        a governance event and requires ``allow_rotate=True``; without it this
        raises, so a silent identity swap is impossible.
        """
        if binding.status not in _STATUSES:
            raise IdentityStoreError(
                f"invalid status {binding.status!r} (expected one of {_STATUSES})")
        with _lock:
            current = self.load()
            existing = current.get(binding.agent_type)
            if existing and existing.principal_id != binding.principal_id and not allow_rotate:
                raise IdentityStoreError(
                    f"{binding.agent_type} is already bound to {existing.principal_id!r}; "
                    f"pass allow_rotate to rebind it to {binding.principal_id!r}"
                )
            if existing and existing.principal_id == binding.principal_id and existing.active:
                return existing
            record = binding if binding.enrolled_at else replace(binding, enrolled_at=utc_now())
            current[record.agent_type] = record
            self._write(current)
            logger.info("identity_store.bound", extra={
                "agent_type": record.agent_type, "principal_id": record.principal_id,
                "rotated": bool(existing), "enrolled_by": record.enrolled_by,
            })
            return record

    def revoke(self, agent_type: str) -> Optional[IdentityBinding]:
        """Mark a binding revoked, so the identity stops resolving without losing
        the audit record of it having existed. Returns ``None`` if not present."""
        with _lock:
            current = self.load()
            existing = current.get(agent_type)
            if existing is None:
                return None
            revoked = replace(existing, status=STATUS_REVOKED)
            current[agent_type] = revoked
            self._write(current)
            logger.warning("identity_store.revoked", extra={
                "agent_type": agent_type, "principal_id": existing.principal_id})
            return revoked

    def _write(self, bindings: dict[str, IdentityBinding]) -> None:
        import yaml
        payload = {
            "version": "1.0",
            "bindings": {k: bindings[k].to_dict() for k in sorted(bindings)},
        }
        body = (
            "# Managed by the Governance Authority Registrar "
            "(governance/remote/registrar.py).\n"
            "# Identity bindings only — capabilities live in the policy registry.\n"
            "# Edit through `galaxy enroll`, not by hand.\n"
            + yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise IdentityStoreError(f"cannot write identity store {self.path}: {e}") from e
