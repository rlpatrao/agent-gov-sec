"""HTTP client for the Galaxy enforcement service.

This is the agent's side of mechanism 4. The enforcement service runs in a
governance-owned environment under an identity the agent cannot assume, and it
re-runs the same ``EnforcementSession`` the in-process pipeline runs, so its
decision is the authoritative one.

Three properties matter more than ergonomics here:

* **Fail closed.** A timeout, a connection error, or an unparseable response is
  raised as :class:`EnforcementUnavailable`. The authority being unreachable is
  not permission to proceed ungoverned.
* **Identity is not the agent's to assert.** ``x-agent-type`` and ``x-nhi-id``
  come from :class:`~galaxy_agentkit.settings.Settings`, which reads the
  deployment's environment. Nothing accepts them as call arguments.
* **Standard library only.** The chokepoint handlers are dependency-light on
  purpose; the client that calls them should not drag in a HTTP stack.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .errors import EnforcementDenied, EnforcementUnavailable
from .settings import Settings

_DENIAL_STATUSES = (400, 401, 403, 404, 409, 422)


class EnforcementClient:
    """Calls the enforcement service's ``/llm``, ``/data`` and ``/a2a`` routes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ── transport ────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            # Resolved from the deployment, never from the caller: the authority
            # keys the governing policy on these.
            "x-agent-type": self._settings.agent_type,
            "x-nhi-id": self._settings.nhi_id,
        }
        if self._settings.token:
            headers["authorization"] = f"Bearer {self._settings.token}"
        return headers

    def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._settings.route(route)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._settings.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = _safe_json(raw)
            if exc.code in _DENIAL_STATUSES:
                raise EnforcementDenied(
                    detail.get("error") or f"http_{exc.code}",
                    detail.get("reason") or raw[:200],
                    route=route,
                ) from None
            # 5xx and anything unexpected: the authority did not render a
            # decision, so there is no decision to act on.
            raise EnforcementUnavailable(
                f"enforcement service returned HTTP {exc.code} for {route}: "
                f"{detail.get('error') or raw[:200]}"
            ) from None
        except urllib.error.URLError as exc:
            raise EnforcementUnavailable(
                f"cannot reach the enforcement service at {url}: {exc.reason}. "
                "Calls are denied while the authority is unreachable."
            ) from None
        except TimeoutError:
            raise EnforcementUnavailable(
                f"enforcement service timed out after {self._settings.timeout}s "
                f"for {route}."
            ) from None

        try:
            return json.loads(body or "{}")
        except ValueError:
            raise EnforcementUnavailable(
                f"enforcement service returned a non-JSON response for {route}."
            ) from None

    # ── routes ───────────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        """Liveness, plus the authority's version and revision."""
        url = self._settings.route("health")
        try:
            with urllib.request.urlopen(url, timeout=self._settings.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise EnforcementUnavailable(
                f"enforcement service health check failed at {url}: {exc}"
            ) from None

    def llm(
        self,
        messages: list[dict[str, Any]],
        *,
        system: list[dict[str, Any]] | None = None,
        tool_config: dict[str, Any] | None = None,
        inference_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Govern and execute an LLM call.

        The model id is pinned server-side; supplying one here has no effect. The
        authority runs the input guards, invokes the model, checks the tool-call
        plan, and redacts the output before it returns.
        """
        payload: dict[str, Any] = {"messages": messages}
        if system:
            payload["system"] = system
        if tool_config:
            payload["toolConfig"] = tool_config
        if inference_config:
            payload["inferenceConfig"] = inference_config
        return self._post("llm", payload)

    def data(
        self, dataset: str, table: str, columns: list[str] | None = None
    ) -> dict[str, Any]:
        """Read through the data chokepoint, with field-grained access applied.

        The agent never supplies rows: the authority reads the source itself and
        returns only what the policy permits, masked and row-filtered.
        """
        return self._post(
            "data", {"dataset": dataset, "table": table, "columns": columns or []}
        )

    def a2a(self, recipient: str) -> dict[str, Any]:
        """Authorize a dispatch to another agent. Raises if it is not permitted."""
        return self._post("a2a", {"recipient": recipient})


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
