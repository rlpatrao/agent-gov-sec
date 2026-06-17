"""
cloud_adapters.gcp.agent_engine — run the governed agents inside Vertex AI Agent Engine.

Vertex AI Agent Engine is a *managed runtime host*: you hand it a Python object
that implements ``set_up()`` and ``query()`` plus ``register_operations()``, it
packages your code + requirements, and runs it as a managed container.

``GalaxyAgentEngineApp`` is that object. It does **not** re-implement anything —
inside the container it builds one of the existing governed LangGraph bundles
(``payload_agents/*``), so the full GuardPipeline (prompt-injection, credential,
context-budget, capability, blocked-pattern, CoT/CoVe trace) and the cloud
hash-chain ledger run exactly as they do locally. With ``CLOUD_PROVIDER=gcp`` the
ledger is BigQuery, identity is the per-agent Service Account, and egress resolves
to Vertex AI.

Design notes:
  - The instance holds only plain, picklable config (Agent Engine cloudpickles it).
    The heavy objects — the model and the per-agent builders — are created in
    ``set_up()``, which runs in the deployed container.
  - A fresh governed bundle (and a fresh run_id) is built per request, so each
    query produces its own tamper-evident hash chain, flushed and verified before
    the response is returned.
  - When no GCP project is configured the model falls back to the offline
    ``FakeToolCallingModel`` (the same env-gated upgrade the demo uses), so the app
    can be smoke-tested locally without Vertex.

See docs/agent-engine.md for deployment.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The three demo personas → their governed-bundle builders. Resolved in set_up().
_AGENTS = ("finops", "auditor", "rogue")


class GalaxyAgentEngineApp:
    """A Vertex AI Agent Engine custom-template app that runs a governed agent."""

    def __init__(
        self,
        *,
        agent: str = "finops",
        project: Optional[str] = None,
        location: Optional[str] = None,
        model_name: Optional[str] = None,
        cloud_provider: str = "gcp",
    ) -> None:
        if agent not in _AGENTS:
            raise ValueError(f"agent must be one of {_AGENTS}, got {agent!r}")
        self._agent = agent
        self._project = project
        self._location = location
        self._model_name = model_name or "gemini-2.5-pro"
        self._cloud_provider = cloud_provider
        # Populated in set_up() (kept off the instance until then so create() stays light).
        self._model = None
        self._builders: dict[str, Any] = {}

    # ── Agent Engine lifecycle ────────────────────────────────────────────────
    def set_up(self) -> None:
        """Runs once when the managed container starts. Resolves cloud bindings,
        builds the model, and wires the per-agent builders."""
        os.environ.setdefault("CLOUD_PROVIDER", self._cloud_provider)
        if self._project:
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", self._project)
        if self._location:
            os.environ.setdefault("VERTEX_AI_LOCATION", self._location)

        from payload_agents._runtime.models import build_gemini_model, scripted_model
        from langchain_core.messages import AIMessage

        # Offline fallback: a single plain turn. before_model/after_model guards
        # still run on every request, so a local (no-project) run exercises the
        # governance stack and the ledger without needing Vertex.
        fallback = scripted_model(AIMessage(content="(offline) governed run complete."))
        self._model = build_gemini_model(
            model=self._model_name, project=self._project, location=self._location,
            offline_fallback=fallback,
        )

        from payload_agents.langgraph import build_finops_agent
        from payload_agents.langgraph import build_auditor_agent
        from payload_agents.langgraph import build_rogue_agent
        self._builders = {
            "finops": build_finops_agent,
            "auditor": build_auditor_agent,
            "rogue": build_rogue_agent,
        }
        logger.info("agent_engine.set_up", extra={
            "agent": self._agent, "cloud": self._cloud_provider,
            "vertex": bool(self._project), "model": self._model_name,
        })

    def query(self, *, prompt: str, agent: Optional[str] = None, run_id: Optional[str] = None) -> dict:
        """The Agent Engine request entrypoint. Builds a governed bundle, runs the
        prompt through every guard, persists + verifies the hash-chain ledger, and
        returns a JSON-serializable result. A blocked request is a normal response
        (blocked=true with the control code), not an error."""
        import asyncio

        if self._model is None:
            self.set_up()
        agent = (agent or self._agent).lower()
        if agent not in self._builders:
            raise ValueError(f"unknown agent {agent!r}; expected one of {_AGENTS}")
        run_id = run_id or f"ae-{agent}-{uuid.uuid4().hex[:12]}"
        return asyncio.run(self._run(prompt=prompt, agent=agent, run_id=run_id))

    def register_operations(self) -> dict:
        """Declare the synchronous operations Agent Engine should expose."""
        return {"": ["query"]}

    # ── internals ─────────────────────────────────────────────────────────────
    async def _run(self, *, prompt: str, agent: str, run_id: str) -> dict:
        from governance.pipeline import GovernanceViolation

        bundle = await self._builders[agent](run_id, self._model)
        verdict: dict = {"blocked": False}
        turns: list[dict] = []
        try:
            result = bundle.invoke(prompt)
            turns = _turns_to_dicts(result)
        except GovernanceViolation as e:
            verdict = {"blocked": True, "code": e.code, "message": str(e)}
            logger.info("agent_engine.blocked", extra={"run_id": run_id, "code": e.code})

        chain_valid: Optional[bool] = None
        try:
            await bundle.pg_backend.flush_async()
            chain_valid = await bundle.pg_backend.verify_chain()
        except Exception as e:  # ledger persistence must not break the response
            logger.warning("agent_engine.ledger_flush_failed", extra={"run_id": run_id, "error": str(e)})
        finally:
            try:
                await bundle.pg_backend.close()
            except Exception:
                pass

        return {
            "run_id": run_id,
            "agent": agent,
            "agent_id": bundle.agent_id,
            "nhi_id": bundle.nhi_id,
            "egress": bundle.egress,
            "verdict": verdict,
            "turns": turns,
            "ledger_chain_valid": chain_valid,
        }


def _turns_to_dicts(result: Any) -> list[dict]:
    """Normalize a RunResult into JSON-serializable turns."""
    out: list[dict] = []
    for t in getattr(result, "turns", []) or []:
        out.append({
            "role": t.role,
            "text": t.text,
            "tool_name": t.tool_name,
            "tool_calls": [{"name": tc.name, "args": tc.args, "id": tc.id} for tc in (t.tool_calls or [])],
        })
    return out
