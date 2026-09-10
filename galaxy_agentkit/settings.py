"""Runtime settings for a governed agent.

Everything the kit needs to govern a call comes from here, and everything here
comes from the environment. That is deliberate: the deployment decides which
authority an agent talks to and which identity it presents, and a developer
cannot override either from inside the agent process.

    GALAXY_ENFORCEMENT_ENDPOINT   base URL of the enforcement service (required
                                  unless mode is `inprocess`)
    GALAXY_AGENT_TYPE             the agent's registered type, e.g. `Payroll`
    GALAXY_NHI_ID                 the agent's Non-Human Identity principal
    GALAXY_MODE                   `remote` (default), `inprocess`, or `both`
    GALAXY_TIMEOUT_SECONDS        per-request timeout (default 30)
    GALAXY_ENFORCEMENT_TOKEN      optional bearer token, when the authority sits
                                  behind a gateway that requires one
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from .errors import ConfigurationError

Mode = Literal["remote", "inprocess", "both"]

_MODES: tuple[str, ...] = ("remote", "inprocess", "both")
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Settings:
    """Resolved, validated settings. Construct with :meth:`from_env`."""

    agent_type: str
    nhi_id: str
    mode: Mode = "remote"
    endpoint: str | None = None
    token: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    @property
    def calls_remote(self) -> bool:
        return self.mode in ("remote", "both")

    @property
    def calls_inprocess(self) -> bool:
        return self.mode in ("inprocess", "both")

    def route(self, name: str) -> str:
        """Absolute URL of an enforcement route (`llm`, `data`, `a2a`, `health`)."""
        if not self.endpoint:
            raise ConfigurationError(
                "no enforcement endpoint configured; set GALAXY_ENFORCEMENT_ENDPOINT"
            )
        return f"{self.endpoint.rstrip('/')}/{name.lstrip('/')}"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """Read and validate settings, raising rather than guessing.

        Every failure here is a deployment mistake that would otherwise surface as
        an ungoverned agent, so each one names the variable that fixes it.
        """
        src = os.environ if env is None else env

        agent_type = (src.get("GALAXY_AGENT_TYPE") or "").strip()
        if not agent_type:
            raise ConfigurationError(
                "GALAXY_AGENT_TYPE is not set. It must be the agent's registered "
                "type (the name used by `galaxy enroll` and by the policy registry), "
                "because the authority resolves the governing policy from it."
            )

        nhi_id = (src.get("GALAXY_NHI_ID") or "").strip()
        if not nhi_id:
            raise ConfigurationError(
                "GALAXY_NHI_ID is not set. It must be the agent's Non-Human Identity "
                "principal; the authority keys the policy registry on it and denies "
                "an unrecognised identity with 403 no_governance_policy."
            )

        mode = (src.get("GALAXY_MODE") or "remote").strip().lower()
        if mode not in _MODES:
            raise ConfigurationError(
                f"GALAXY_MODE is {mode!r}; expected one of {', '.join(_MODES)}."
            )

        endpoint = (src.get("GALAXY_ENFORCEMENT_ENDPOINT") or "").strip() or None
        if mode in ("remote", "both"):
            if not endpoint:
                raise ConfigurationError(
                    "GALAXY_ENFORCEMENT_ENDPOINT is not set, but GALAXY_MODE is "
                    f"{mode!r}. Point it at the enforcement service, for example "
                    "http://localhost:8080 for the local compose stack. To run guards "
                    "in-process only — which is defence in depth, not an authority — "
                    "set GALAXY_MODE=inprocess."
                )
            parsed = urlparse(endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ConfigurationError(
                    f"GALAXY_ENFORCEMENT_ENDPOINT is {endpoint!r}, which is not an "
                    "absolute http(s) URL."
                )

        raw_timeout = (src.get("GALAXY_TIMEOUT_SECONDS") or "").strip()
        if raw_timeout:
            try:
                timeout = float(raw_timeout)
            except ValueError:
                raise ConfigurationError(
                    f"GALAXY_TIMEOUT_SECONDS is {raw_timeout!r}, which is not a number."
                ) from None
            if timeout <= 0:
                raise ConfigurationError(
                    f"GALAXY_TIMEOUT_SECONDS is {timeout}; it must be positive."
                )
        else:
            timeout = DEFAULT_TIMEOUT

        return cls(
            agent_type=agent_type,
            nhi_id=nhi_id,
            mode=mode,  # type: ignore[arg-type]
            endpoint=endpoint,
            token=(src.get("GALAXY_ENFORCEMENT_TOKEN") or "").strip() or None,
            timeout=timeout,
        )
