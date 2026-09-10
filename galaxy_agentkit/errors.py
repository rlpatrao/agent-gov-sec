"""Exceptions raised by the agentkit.

All of them are fatal by design. The kit governs agents, so a condition it cannot
resolve — missing configuration, an unreachable authority, a denied call — is
never downgraded to a warning and never falls back to a weaker default. Callers
that genuinely want a soft failure must catch these explicitly, which makes the
decision visible in their code and in review.
"""

from __future__ import annotations


class GalaxyError(Exception):
    """Base class for every agentkit failure."""


class ConfigurationError(GalaxyError):
    """Required governance configuration is missing, unreadable, or incomplete.

    Raised at construction rather than at first use: an agent that cannot load
    the configuration governing it must not start, because the alternative is a
    process that looks governed and is not.
    """


class EnforcementUnavailable(GalaxyError):
    """The enforcement service could not be reached or returned a transport error.

    Fail-closed: the caller must treat this as a denial. The authority being down
    is not permission to proceed ungoverned.
    """


class EnforcementDenied(GalaxyError):
    """The enforcement service denied the call.

    ``code`` is the machine-readable control code (for example
    ``prompt_injection``, ``no_governance_policy``, ``data_access_denied``) and
    ``reason`` the human-readable explanation the authority returned.
    """

    def __init__(self, code: str, reason: str = "", *, route: str = "") -> None:
        self.code = code
        self.reason = reason
        self.route = route
        detail = f"{code}: {reason}" if reason else code
        super().__init__(f"denied at {route}: {detail}" if route else detail)
