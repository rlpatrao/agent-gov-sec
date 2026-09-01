"""
governance/remote/server.py — the Governance Authority service (mechanism 4).

A small, dependency-light HTTP server that mounts the governance chokepoints and
the identity control plane behind stable routes.

Data plane — enforcement, called on every agent request:

  POST /llm     → LLM-egress enforcement (input guards · tool-plan · output redaction)
  POST /data    → data-access enforcement (field-grained masking · row filter · deny)
  POST /a2a      → agent-to-agent authorization (recipient allow-list)
  GET  /health  → liveness, plus the image version and revision

Operational view:

  GET  /dashboard → the Governance Dashboard (see docs/shared/dashboard.md)

Control plane — identity enrollment, called rarely and by humans:

  POST /enroll           → record an agent_type → cloud principal binding (Registrar)
  GET  /identity         → resolve a binding (?agent_type=FinOps)
  GET  /registry/digest  → the deployed policy registry's digest, for agent preflight

The two planes are separated on purpose. Enforcement is a hot path that must never
mutate authority state; enrollment mutates state and therefore requires a human
credential. The control plane is **disabled unless ``GOV_CONTROL_TOKEN`` is set**,
so the default posture of a deployed enforcement service is data plane only.

This is the governance-owned runnable — the runtime companion to CODEOWNERS. It
runs in a separate environment under a separate identity the agent cannot assume,
so the controls hold even if the agent runtime is hostile. The *same* image is
used in local development (via deploy/docker-compose.yml) and in production, so
what a developer tests against is what enforces in production (dev/prod parity).

It reuses the exact chokepoint handlers the serverless deployments use
(``cloud_adapters/aws/infra/lambda/*``), which in turn call the shared
``governance.remote.enforce`` library — the single enforcement code path. The data
plane needs only the stdlib; a handler lazy-imports its own cloud SDK when (and
if) it needs one, so ``/data`` and ``/a2a`` run with no cloud dependencies at all.

Where the enrolling identity comes from
---------------------------------------
``POST /enroll`` must attribute the request to a *human* principal, and it cannot
take that principal's word for it from the request body. The caller ARN is read
from ``GOV_CALLER_ARN_HEADER`` (default ``x-amzn-iam-caller-arn``), which is
expected to be populated by an IAM-authorizing front door — API Gateway or an ALB
with ``AWS_IAM`` authorization — that has already validated the caller's SigV4
signature. Without such a front door the header is unverified input, so the server
refuses to honour it unless ``GOV_CONTROL_TRUST_HEADER=1`` is set explicitly for
local development, and logs a warning on every request when it is.

For the governing team and CI, prefer ``galaxy enroll`` in direct mode: it runs the
Registrar in-process against the developer's own AWS SSO session, so the actor is
established by AWS rather than asserted over HTTP.

Run:
    python -m governance.remote.server            # binds 0.0.0.0:8080
    GOV_ENFORCE_PORT=9000 python -m governance.remote.server
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from governance.remote.decision_log import DECISIONS

logger = logging.getLogger(__name__)

# Stamped into the image at build time from deploy/VERSION (see
# scripts/publish_service_image.py). A build that did not go through the publish
# path reports the default, so an unversioned image is visible as such.
_DEV_VERSION = "0.0.0-dev"


def service_version() -> str:
    """The running service's version, as stamped into the image."""
    return os.environ.get("GALAXY_SERVICE_VERSION") or _DEV_VERSION


def service_revision() -> str:
    """The git revision the running image was built from."""
    return os.environ.get("GALAXY_SERVICE_REVISION") or "unknown"

# route → chokepoint handler filename. The chokepoint modules are loaded as
# top-level modules by file path (exactly as AWS Lambda loads them) — the source
# directory (`.../infra/lambda/`) is not an importable package and its name is a
# Python keyword, so a dotted import will not work.
_ROUTES = {"/llm": "bedrock_proxy.py", "/data": "data_proxy.py", "/a2a": "a2a_broker.py"}
_handler_cache: dict[str, object] = {}

# Candidate directories: an explicit override, the repo tree, then the container
# task root (the Dockerfile copies the handlers to the working directory).
_REPO = Path(__file__).resolve().parents[2]
_CANDIDATE_DIRS = [
    Path(os.environ["GOV_HANDLER_DIR"]) if os.environ.get("GOV_HANDLER_DIR") else None,
    _REPO / "cloud_adapters" / "aws" / "infra" / "lambda",
    Path.cwd(),
]


def _resolve(route: str):
    if route not in _handler_cache:
        filename = _ROUTES[route]
        path = next((d / filename for d in _CANDIDATE_DIRS if d and (d / filename).exists()), None)
        if path is None:
            raise FileNotFoundError(f"chokepoint handler {filename} not found in {[str(d) for d in _CANDIDATE_DIRS if d]}")
        spec = importlib.util.spec_from_file_location(f"_gov_choke_{route.strip('/')}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _handler_cache[route] = mod.handler
    return _handler_cache[route]


# ── Control plane ─────────────────────────────────────────────────────────────

_CALLER_ARN_HEADER = (os.environ.get("GOV_CALLER_ARN_HEADER") or "x-amzn-iam-caller-arn").lower()


def _control_token() -> str:
    return os.environ.get("GOV_CONTROL_TOKEN") or ""


def _control_authorized(headers: dict) -> bool:
    """Constant-time bearer check. The control plane stays closed when no token is
    configured, so a service deployed with data-plane config alone cannot enroll.

    KNOWN GAP (I1 in docs/shared/agent-registration-plan.md): this one token gates
    both ``GET /identity`` — which agents call to resolve their NHI — and
    ``POST /enroll``. An agent holding the read token therefore holds the secret
    that also gates enrollment, and with ``GOV_CONTROL_TRUST_HEADER=1`` it could
    enroll with a forged caller ARN. Split into separate read and write tokens
    before deploying the control plane outside local development.
    """
    token = _control_token()
    if not token:
        return False
    presented = (headers.get("authorization") or "").removeprefix("Bearer ").strip()
    return bool(presented) and hmac.compare_digest(presented, token)


def _registry() -> dict:
    """Load the deployed policy registry using the same contract the chokepoint
    handlers use, so the control plane reports readiness against exactly the
    artifact that enforces."""
    from governance.shared.policy_registry import load_registry
    raw = os.environ.get("GOV_POLICY_REGISTRY")
    if not raw:
        path = os.environ.get("GOV_POLICY_REGISTRY_PATH")
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
    return load_registry(raw) if raw else {}


def _registry_digest() -> dict:
    """A stable digest of the deployed registry, for agent-side preflight. Lists
    the agent types present but no policy content — an agent learns whether it is
    covered, not what any other agent is permitted to do."""
    reg = _registry()
    canonical = json.dumps(reg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "version": reg.get("version", ""),
        "default": reg.get("default", "deny"),
        "agent_types": sorted((reg.get("agents") or {}).keys()),
    }


def _caller_identity_from_headers(headers: dict):
    """Build a CallerIdentity from the front door's verified caller header.

    Raises ``EnrollmentDenied`` when the header is absent, or present but
    untrusted because no IAM-authorizing front door is declared."""
    from governance.remote.registrar import CallerIdentity, EnrollmentDenied

    arn = (headers.get(_CALLER_ARN_HEADER) or "").strip()
    if not arn:
        raise EnrollmentDenied(
            f"no verified caller identity ({_CALLER_ARN_HEADER} absent). Front the "
            f"control plane with an IAM-authorizing gateway, or use `galaxy enroll` "
            f"in direct mode against your AWS SSO session."
        )
    if os.environ.get("GOV_CONTROL_TRUST_HEADER") != "1":
        raise EnrollmentDenied(
            f"{_CALLER_ARN_HEADER} is present but not trusted: no IAM-authorizing "
            f"front door is declared. Set GOV_CONTROL_TRUST_HEADER=1 only for local "
            f"development."
        )
    logger.warning("control.trusting_unverified_caller_header", extra={"arn": arn})
    account = arn.split(":")[4] if arn.count(":") >= 5 else ""
    return CallerIdentity(arn=arn, account=account)


def _handle_enroll(headers: dict, body: dict) -> tuple[int, dict]:
    """``POST /enroll`` — record an identity binding via the Registrar."""
    from governance.remote.registrar import EnrollmentDenied, enroll

    try:
        caller = _caller_identity_from_headers(headers)
        agent_type = str(body.get("agent_type") or "")
        principal_id = str(body.get("principal_id") or "")
        verifier = _build_verifier(caller)
        result = enroll(
            agent_type, principal_id,
            verifier=verifier, registry=_registry(),
            allow_rotate=bool(body.get("allow_rotate")),
        )
    except EnrollmentDenied as e:
        logger.warning("control.enroll_denied", extra={"reason": str(e)[:300]})
        return 403, {"error": "enrollment_denied", "reason": str(e)}
    except Exception as e:
        logger.exception("control.enroll_error")
        return 500, {"error": "enrollment_failed", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    return 200, result.to_dict()


def _build_verifier(caller):
    """A PrincipalVerifier that reports the front-door-verified caller but performs
    principal existence checks with the authority's own read-only credentials."""
    from governance.remote.registrar import EnrollmentDenied, VerifiedPrincipal

    class _AuthorityVerifier:
        def caller_identity(self):
            return caller

        def verify_principal(self, *, agent_type: str, principal_id: str) -> VerifiedPrincipal:
            try:
                from cloud_adapters.aws.principal_verify import AwsPrincipalVerifier
            except ImportError:
                raise EnrollmentDenied(
                    "no principal verifier available for this deployment; the "
                    "authority cannot confirm the principal exists"
                )
            return AwsPrincipalVerifier().verify_principal(
                agent_type=agent_type, principal_id=principal_id)

    return _AuthorityVerifier()


class _Handler(BaseHTTPRequestHandler):
    server_version = f"GalaxyEnforcement/{service_version()}"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> dict:
        return {k.lower(): v for k, v in self.headers.items()}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route == "/health":
            self._send(200, {
                "status": "ok",
                "service": "galaxy-enforcement",
                "version": service_version(),
                "revision": service_revision(),
                "control_plane": "enabled" if _control_token() else "disabled",
            })
            return
        if route == "/dashboard":
            from governance.remote.dashboard import render_dashboard
            page = render_dashboard(
                version=service_version(), revision=service_revision(),
                uptime=DECISIONS.uptime(), registry=_registry(), log=DECISIONS,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if route == "/registry/digest":
            self._send(200, _registry_digest())
            return
        if route == "/identity":
            if not _control_authorized(self._headers()):
                self._send(403, {"error": "control_plane_unauthorized"})
                return
            from governance.remote.registrar import resolve_identity
            agent_type = (parse_qs(parsed.query).get("agent_type") or [""])[0]
            resolved = resolve_identity(agent_type, registry=_registry())
            if resolved is None:
                self._send(404, {"error": "no_identity_binding", "agent_type": agent_type})
            else:
                self._send(200, resolved)
            return
        self._send(404, {"error": "not_found", "path": self.path})

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("content-length") or 0)

        if route == "/enroll":
            if not _control_authorized(self._headers()):
                self._send(403, {"error": "control_plane_unauthorized",
                                 "reason": "control plane is disabled or the bearer token is invalid"})
                return
            body_raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                body = json.loads(body_raw) or {}
            except ValueError:
                self._send(400, {"error": "invalid_json"})
                return
            status, payload = _handle_enroll(self._headers(), body)
            self._send(status, payload)
            return

        if route not in _ROUTES:
            self._send(404, {"error": "not_found", "path": self.path})
            return
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        # Adapt the HTTP request to the API-Gateway/Lambda event the handlers expect.
        event = {"headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw}
        try:
            resp = _resolve(route)(event, None)
        except Exception as e:  # fail closed — never leak an unhandled error as success
            logger.exception("enforcement.handler_error", extra={"route": route})
            self._send(502, {"error": "enforcement_unavailable", "detail": f"{type(e).__name__}: {str(e)[:200]}"})
            return
        status = int(resp.get("statusCode", 200))
        try:
            payload = json.loads(resp.get("body") or "{}")
        except (TypeError, ValueError):
            payload = {"body": resp.get("body")}
        DECISIONS.record(route=route, agent_type=self.headers.get("x-agent-type"),
                         nhi_id=self.headers.get("x-nhi-id"), status=status, payload=payload)
        self._send(status, payload)

    def log_message(self, fmt, *args):  # route access logs through logging, not stderr
        logger.info("enforcement.request %s :: %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=os.environ.get("GALAXY_LOG_LEVEL", "INFO"))
    host = os.environ.get("GOV_ENFORCE_HOST", "0.0.0.0")
    port = int(os.environ.get("GOV_ENFORCE_PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), _Handler)
    control = "enabled" if _control_token() else "disabled"
    logger.info("enforcement.listening", extra={
        "host": host, "port": port, "routes": sorted(_ROUTES), "control_plane": control,
        "version": service_version(), "revision": service_revision()})
    if control == "enabled" and os.environ.get("GOV_CONTROL_TRUST_HEADER") == "1":
        logger.warning("control.header_trust_enabled_do_not_use_in_production")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
