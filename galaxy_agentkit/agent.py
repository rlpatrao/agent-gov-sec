"""The wrapper a developer puts around an agent.

One object, obtained from :func:`govern`, carries everything an agent needs to
run under governance: the resolved identity, the authority client, and — when
asked for — the in-process guard pipeline that provides defence in depth.

    from galaxy_agentkit import govern

    agent = govern()
    reply = agent.llm([{"role": "user", "content": [{"text": prompt}]}])

Three things are settled before :func:`govern` returns, so that an agent which
cannot be governed does not start:

1. settings resolve and validate (identity, endpoint, mode);
2. the governance configuration bundle is present and complete;
3. in ``remote``/``both`` mode the authority answers its health check, and its
   version is recorded so the run can be tied to the build that enforced it.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .client import EnforcementClient
from .config import verify_bundle
from .errors import ConfigurationError
from .settings import Settings

logger = logging.getLogger(__name__)


class GovernedAgent:
    """A governed agent handle. Construct with :func:`govern`."""

    def __init__(
        self,
        settings: Settings,
        *,
        run_id: str | None = None,
        verify_authority: bool = True,
    ) -> None:
        self.settings = settings
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self._client = EnforcementClient(settings) if settings.calls_remote else None
        self._pipeline: Any | None = None
        self.authority: dict[str, Any] = {}

        # Configuration first: a degraded guard is worse than a missing one,
        # because it still reports success.
        verify_bundle()

        if self._client is not None and verify_authority:
            self.authority = self._client.health()
            logger.info(
                "galaxy.authority_connected",
                extra={
                    "endpoint": settings.endpoint,
                    "authority_version": self.authority.get("version"),
                    "agent_type": settings.agent_type,
                    "run_id": self.run_id,
                },
            )

    # ── authority-backed calls ───────────────────────────────────────────
    @property
    def client(self) -> EnforcementClient:
        """The enforcement client, or an error explaining why there is none."""
        if self._client is None:
            raise ConfigurationError(
                "this agent runs with GALAXY_MODE=inprocess, so it has no "
                "enforcement client. Set GALAXY_MODE=remote (or both) and "
                "GALAXY_ENFORCEMENT_ENDPOINT to call the authority."
            )
        return self._client

    def llm(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        """Run an LLM call through the authority. Raises if it is denied."""
        return self.client.llm(messages, **kwargs)

    def data(
        self, dataset: str, table: str, columns: list[str] | None = None
    ) -> dict[str, Any]:
        """Read data through the authority, with field-grained access applied."""
        return self.client.data(dataset, table, columns)

    def a2a(self, recipient: str) -> dict[str, Any]:
        """Authorize a dispatch to another agent. Raises if it is not permitted."""
        return self.client.a2a(recipient)

    def health(self) -> dict[str, Any]:
        """Re-check the authority's liveness and version."""
        return self.client.health()

    # ── in-process defence in depth ──────────────────────────────────────
    @property
    def pipeline(self) -> Any:
        """The in-process :class:`GuardPipeline`, built lazily.

        This is defence in depth, not the authority: it runs inside the agent's
        own trust domain, so a hostile runtime could bypass it. The chokepoint
        decision is the one that counts.
        """
        if not self.settings.calls_inprocess:
            raise ConfigurationError(
                "in-process guards are disabled; set GALAXY_MODE=inprocess or "
                "both to build the local GuardPipeline."
            )
        if self._pipeline is None:
            from agent_os.audit_logger import GovernanceAuditLogger
            from governance.shared.enforcement.pipeline import GuardPipeline

            self._pipeline = GuardPipeline(
                agent_id=f"{self.settings.agent_type.lower()}-{self.run_id}",
                agent_type=self.settings.agent_type,
                nhi_id=self.settings.nhi_id,
                run_id=self.run_id,
                audit_log=GovernanceAuditLogger(),
            )
        return self._pipeline

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        version = self.authority.get("version", "-")
        return (
            f"<GovernedAgent {self.settings.agent_type} mode={self.settings.mode} "
            f"authority={version} run={self.run_id}>"
        )


def govern(
    *,
    settings: Settings | None = None,
    run_id: str | None = None,
    verify_authority: bool = True,
) -> GovernedAgent:
    """Return a governed handle for this agent.

    Reads configuration from the environment unless `settings` is supplied, and
    raises rather than degrading if anything required is absent. Pass
    ``verify_authority=False`` only for offline unit tests — it skips the health
    check, so nothing has confirmed an authority is actually there.
    """
    return GovernedAgent(
        settings or Settings.from_env(),
        run_id=run_id,
        verify_authority=verify_authority,
    )
