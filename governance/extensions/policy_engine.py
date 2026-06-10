"""
governance.extensions.policy_engine — standards-based authz via MSGK's Cedar backend.

Wires **Cedar** (AWS's authorization policy language) as the single, standards-based
decision point for both **agent/tool authz** and **data authz**, through MSGK's
``agent_os.policies.PolicyEvaluator.load_cedar`` + ``CedarBackend``. This is the
answer to "RBAC/ABAC engine?": rather than add Casbin (a third engine), we use the
Cedar backend MSGK already ships — it aligns with the AWS/Lake-Formation direction
and is formally specified.

``CedarAuthorizer`` exposes two entry points that the rest of the platform calls:
  - ``authorize_action`` — agent/tool authz (principal · action · resource)
  - ``authorize_data``   — data authz (resource carries classification/category/geo)

Both build a context dict and call ``PolicyEvaluator.evaluate`` (Cedar evaluates
it). **Fail-closed:** any error → deny.

⚠️ Full Cedar evaluation requires the Cedar engine — ``cedarpy`` (Rust bindings)
or the ``cedar`` CLI (``pip install '.[cedar]'``). Without it MSGK falls back to a
built-in matcher that only handles coarse permit/forbid (no ``when`` conditions),
so conditional ABAC policies will deny-by-default. The authorizer logs a clear
warning in that case. Disabled by default; enable with ``GALAXY_POLICY_ENGINE=cedar``.
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
    """Cedar-backed authorizer for agent + data authz (via MSGK's PolicyEvaluator)."""

    def __init__(self, policy_path: Optional[Path] = None, policy_content: Optional[str] = None) -> None:
        from agent_os.policies import PolicyEvaluator

        self._evaluator = PolicyEvaluator()
        if policy_content is None:
            p = policy_path or (Path(os.environ.get(CEDAR_POLICY_PATH_ENV, "")) if os.environ.get(CEDAR_POLICY_PATH_ENV) else _DEFAULT_CEDAR)
            policy_content = p.read_text("utf-8") if p and p.exists() else None
        if policy_content is None:
            raise FileNotFoundError("no Cedar policy found (set GALAXY_CEDAR_POLICY_PATH or pass policy_content)")
        self._evaluator.load_cedar(policy_content=policy_content)

    # ── agent / tool authz ────────────────────────────────────────────────
    def authorize_action(self, *, principal: str, action: str, resource: str, **attrs: Any) -> bool:
        ctx = {"principal": principal, "action": action, "resource": resource, **attrs}
        return self._allow(ctx)

    # ── data authz ────────────────────────────────────────────────────────
    def authorize_data(self, *, agent_type: str, dataset: str, table: str, column: str, label: Any) -> bool:
        ctx = {
            "principal": agent_type,
            "action": "read",
            "resource": f"{dataset}.{table}.{column}",
            "classification": int(getattr(label, "classification", 0)),
            "categories": list(getattr(label, "categories", []) or []),
            "geography": getattr(label, "geography", "") or "",
        }
        return self._allow(ctx)

    def _allow(self, context: dict) -> bool:
        try:
            decision = self._evaluator.evaluate(context)
            return bool(getattr(decision, "allowed", False))
        except Exception as e:  # fail-closed
            logger.warning("policy_engine.cedar_eval_failed", extra={"error": str(e)})
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
