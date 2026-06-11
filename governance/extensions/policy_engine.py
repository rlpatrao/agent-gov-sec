"""
governance.extensions.policy_engine — standards-based authz via **Cedar**.

Wires Cedar (AWS's authorization policy language) as the single, standards-based
decision point for both **agent/tool authz** and **data authz**. This is the
answer to "RBAC/ABAC engine?": rather than add Casbin (a redundant third engine),
we use Cedar — it aligns with the AWS/Lake-Formation direction and is formally
specified.

``CedarAuthorizer`` exposes two entry points the platform calls:
  - ``authorize_action`` — agent/tool authz (principal · action · resource)
  - ``authorize_data``   — data authz (ABAC over the MSGK ``DataLabel``: classification…)

Both build a Cedar request and evaluate it **fail-closed** (any error → deny).

> **Why cedarpy directly, not MSGK's ``CedarBackend``?** MSGK 3.7.0's Cedar
> backend targets the cedarpy **3.x** API (``cedarpy.AuthorizationRequest``), but
> only cedarpy **4.x** ships a wheel for this Python, and — worse — MSGK's backend
> **fails open** (returns *allow*) when the cedarpy call errors. So we evaluate
> against ``cedarpy.is_authorized`` (4.x) directly, fail-closed. The Cedar policy
> set and the ABAC intent are unchanged; only the evaluation call differs. (MSGK
> incompatibility + fail-open is worth reporting upstream.)

``cedarpy`` is a base dependency, so the engine is available out of the box.
Disabled by default; enable with ``GALAXY_POLICY_ENGINE=cedar``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

POLICY_ENGINE_ENV = "GALAXY_POLICY_ENGINE"          # set to "cedar" to enable
CEDAR_POLICY_PATH_ENV = "GALAXY_CEDAR_POLICY_PATH"  # path to a .cedar file
_DEFAULT_CEDAR = Path(__file__).parent / "configs" / "authz.cedar"


class CedarAuthorizer:
    """Cedar-backed authorizer for agent + data authz (evaluated via cedarpy)."""

    def __init__(self, policy_path: Optional[Path] = None, policy_content: Optional[str] = None) -> None:
        import cedarpy  # base dependency; required for the standards-based engine
        self._cedarpy = cedarpy
        if policy_content is None:
            env = os.environ.get(CEDAR_POLICY_PATH_ENV)
            p = policy_path or (Path(env) if env else _DEFAULT_CEDAR)
            policy_content = p.read_text("utf-8") if p and p.exists() else None
        if policy_content is None:
            raise FileNotFoundError("no Cedar policy found (set GALAXY_CEDAR_POLICY_PATH or pass policy_content)")
        self._policies = policy_content

    # ── agent / tool authz ────────────────────────────────────────────────
    def authorize_action(self, *, principal: str, action: str, resource: str, **context: Any) -> bool:
        rtype = "Tool" if action == "use_tool" else "Resource"
        return self._decide(principal, action, rtype, resource, context)

    # ── data authz (ABAC over the data label) ─────────────────────────────
    def authorize_data(self, *, agent_type: str, dataset: str, table: str, column: str, label: Any) -> bool:
        context = {
            "classification": int(getattr(label, "classification", 0)),
            "categories": list(getattr(label, "categories", []) or []),
            "geography": getattr(label, "geography", "") or "",
        }
        return self._decide(agent_type, "read", "Resource", f"{dataset}.{table}.{column}", context)

    def _decide(self, principal: str, action: str, rtype: str, resource: str, context: dict) -> bool:
        request = {
            "principal": f'Agent::"{principal}"',
            "action": f'Action::"{action}"',
            "resource": f'{rtype}::"{resource}"',
            "context": context,
        }
        try:
            result = self._cedarpy.is_authorized(request, self._policies, entities=[])
            return result.decision == self._cedarpy.Decision.Allow
        except Exception as e:  # fail-closed
            logger.warning("policy_engine.cedar_eval_failed", extra={"error": str(e), "principal": principal})
            return False


def build_authorizer() -> Optional[CedarAuthorizer]:
    """Return a ``CedarAuthorizer`` when ``GALAXY_POLICY_ENGINE=cedar``; else ``None``
    (the mediator/guard then use MSGK's native ABAC evaluator + capability allow-list)."""
    if os.environ.get(POLICY_ENGINE_ENV, "").strip().lower() != "cedar":
        return None
    try:
        return CedarAuthorizer()
    except Exception as e:
        logger.warning("policy_engine.cedar_unavailable", extra={"error": str(e)})
        return None
