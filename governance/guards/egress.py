"""
Egress allow-list — reference implementation around `agent_os.egress_policy.EgressPolicy`.

Today's agents are pure-LLM (Scanner + AST) and don't make outbound network
calls beyond APIM/Azure OpenAI. The egress allow-list is therefore a
declarative reference: it documents the permitted destinations and is
loaded eagerly so any future tool that adds outbound HTTP can call
`check_outbound(url)` instead of speaking to arbitrary hosts.

When a tool-using agent (Coder, Reviewer) lands, the same `EgressPolicy`
instance gets passed into a `FunctionMiddleware` that intercepts every
HTTP-shaped tool call.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agent_os.egress_policy import EgressDecision, EgressPolicy

logger = logging.getLogger(__name__)


def load_egress_policy(yaml_path: Optional[Path] = None) -> EgressPolicy:
    """Load the project's egress allow-list. Default-deny if no rules."""
    policy = EgressPolicy(default_action="deny")
    if yaml_path is None:
        yaml_path = Path(__file__).parent.parent / "configs" / "galaxy-egress.yaml"
    if yaml_path.exists():
        try:
            policy.load_from_yaml(yaml_path.read_text(encoding="utf-8"))
            logger.info("egress.policy_loaded", extra={"path": str(yaml_path)})
        except Exception as e:
            logger.warning("egress.policy_load_failed", extra={"path": str(yaml_path), "error": str(e)})
    else:
        logger.info("egress.policy_default_deny", extra={"reason": f"no file at {yaml_path}"})
    return policy


def check_outbound(policy: EgressPolicy, url: str) -> EgressDecision:
    """Convenience wrapper for tool-call middleware once it lands."""
    return policy.check_url(url)
