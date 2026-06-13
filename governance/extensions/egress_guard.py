"""
governance.extensions.egress_guard — per-call egress allow-list guard.

Wraps ``agent_os.egress_policy.EgressPolicy`` (built fail-closed with
``default_action='deny'`` from a flat ``rules:`` YAML at
``governance/configs/egress-gap.yaml``). The guard inspects network-shaped tool
calls, extracts a URL/host from the arguments, and asks ``EgressPolicy.check_url``
whether the destination is allow-listed. A non-matching host (or any host while
the default action is deny) yields ``GuardDecision(allowed=False,
code='egress_denied')``. Non-network tools are allowed unconditionally.

Quirks honored (per discovery notes):
  - ``load_from_yaml`` is a hand-rolled mini-parser that understands only the
    flat ``rules:`` shape and silently ignores anything else, so the constructor
    feeds it exactly that file.
  - ``EgressDecision`` carries no severity/code field, so the wrapper synthesizes
    the stable ``egress_denied`` code itself.
  - ``check_url`` resolves a missing port to 443 (https) / 80 (otherwise) and a
    non-http scheme to ``hostname=''`` (which then hits the default deny). The
    wrapper passes the raw URL straight through and lets the policy decide.

The wrapper is flag-agnostic and never imports the pipeline; the pipeline gates
it behind ``GALAXY_GAP_EGRESS_POLICY`` and maps a block onto GovernanceViolation.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from agent_os.egress_policy import EgressPolicy

from governance.extensions.decision import GuardDecision

# Default location of the allow-list YAML (flat ``rules:`` shape).
_DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "governance",
    "configs",
    "egress-gap.yaml",
)

# Argument keys that may carry a network destination, in priority order.
_URL_KEYS = ("url", "endpoint", "uri", "host", "hostname", "address", "target")

# Tool-name substrings that mark a network-shaped tool. A tool whose call exposes
# a destination via one of _URL_KEYS is always checked; this list catches
# network tools whose destination travels under a less obvious key.
_NETWORK_TOOL_HINTS = (
    "http",
    "fetch",
    "request",
    "curl",
    "wget",
    "download",
    "webhook",
    "api_call",
    "url",
    "browse",
)


class EgressGuard:
    """Builds one fail-closed EgressPolicy and checks network tool calls against it."""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._config_path = config_path or _DEFAULT_CONFIG
        self._policy = EgressPolicy(default_action="deny")
        with open(self._config_path, "r", encoding="utf-8") as fh:
            # load_from_yaml only understands the flat ``rules:`` shape; pass the
            # file text verbatim and let it ignore anything it does not parse.
            self._policy.load_from_yaml(fh.read())

    @staticmethod
    def _extract_destination(args: dict[str, Any]) -> Optional[str]:
        if not isinstance(args, dict):
            return None
        for key in _URL_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @classmethod
    def _is_network_tool(cls, name: str, destination: Optional[str]) -> bool:
        if destination is not None:
            return True
        lowered = (name or "").lower()
        return any(hint in lowered for hint in _NETWORK_TOOL_HINTS)

    def check_tool(self, name: str, args: dict[str, Any]) -> GuardDecision:
        """Return a GuardDecision for a tool call; allow non-network tools."""
        destination = self._extract_destination(args or {})

        if not self._is_network_tool(name, destination):
            return GuardDecision.allow(
                reason=f"tool {name!r} is not network-shaped; egress check skipped",
                tool=name,
            )

        if destination is None:
            # Network-shaped by name but no destination argument present; nothing
            # to evaluate, so allow and let other guards handle malformed calls.
            return GuardDecision.allow(
                reason=f"no destination argument found for network tool {name!r}",
                tool=name,
            )

        decision = self._policy.check_url(destination)
        if decision.allowed:
            return GuardDecision.allow(
                reason=decision.reason,
                tool=name,
                destination=destination,
            )

        return GuardDecision.block(
            "egress_denied",
            f"egress to {destination!r} blocked: {decision.reason}",
            signals=["default_deny"],
            tool=name,
            destination=destination,
        )
