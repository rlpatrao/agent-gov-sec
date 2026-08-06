"""
governance/remote/server.py — the out-of-process enforcement service (mechanism 4).

A small, dependency-light HTTP server that mounts the three governance
chokepoints behind stable routes:

  POST /llm     → LLM-egress enforcement (input guards · tool-plan · output redaction)
  POST /data    → data-access enforcement (field-grained masking · row filter · deny)
  POST /a2a      → agent-to-agent authorization (recipient allow-list)
  GET  /health  → liveness

This is the governance-owned runnable — the runtime companion to CODEOWNERS. It
runs in a separate environment under a separate identity the agent cannot assume,
so the controls hold even if the agent runtime is hostile. The *same* image is
used in local development (via deploy/docker-compose.yml) and in production, so
what a developer tests against is what enforces in production (dev/prod parity).

It reuses the exact chokepoint handlers the serverless deployments use
(``cloud_adapters/aws/infra/lambda/*``), which in turn call the shared
``governance.remote.enforce`` library — the single enforcement code path. Only
the stdlib is required to run the server; a handler lazy-imports its own cloud
SDK when (and if) it needs one, so ``/data`` and ``/a2a`` run with no cloud
dependencies at all.

Run:
    python -m governance.remote.server            # binds 0.0.0.0:8080
    GOV_ENFORCE_PORT=9000 python -m governance.remote.server
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

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


class _Handler(BaseHTTPRequestHandler):
    server_version = "GalaxyEnforcement/1.0"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "service": "galaxy-enforcement"})
        else:
            self._send(404, {"error": "not_found", "path": self.path})

    def do_POST(self):  # noqa: N802
        route = self.path.rstrip("/") or "/"
        if route not in _ROUTES:
            self._send(404, {"error": "not_found", "path": self.path})
            return
        length = int(self.headers.get("content-length") or 0)
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
        self._send(status, payload)

    def log_message(self, fmt, *args):  # route access logs through logging, not stderr
        logger.info("enforcement.request %s :: %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=os.environ.get("GALAXY_LOG_LEVEL", "INFO"))
    host = os.environ.get("GOV_ENFORCE_HOST", "0.0.0.0")
    port = int(os.environ.get("GOV_ENFORCE_PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), _Handler)
    logger.info("enforcement.listening", extra={"host": host, "port": port, "routes": sorted(_ROUTES)})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
