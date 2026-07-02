"""
cloud_adapters/aws/agentcore/runtime_agent.py — the persona agent hosted on
AgentCore Runtime.

This is the code artifact deployed (one AgentCore Runtime per persona:
``galaxy_finops`` / ``galaxy_auditor`` / ``galaxy_rogue``) so each agent is a
first-class, observable Runtime in the AgentCore console rather than a local
process. The deploy is in ``scripts/deploy_agentcore.py``.

Contract (AgentCore Runtime, ``serverProtocol=HTTP``): an HTTP server on
``0.0.0.0:8080`` exposing ``GET /ping`` (liveness) and ``POST /invocations``
(one turn). We implement it with the standard library so the code artifact needs
no bundled dependencies; only ``botocore`` (present in the managed Python
runtime) is used, to SigV4-sign the gateway call.

What a turn does — the focus is observability and per-agent security, not agent
function: the handler issues a single MCP ``tools/call`` to the **existing
governance gateway** (``GW_URL``) under the Runtime's own execution-role
identity. Because the gateway is AWS_IAM-authed and ENFORCE-bound to the Cedar
policy engine, the call is authorized per agent (Cedar) and content-screened
(the request/response interceptors) at the boundary — so an allow for FinOps and
a forbid for Rogue are observed on the same code path. The tool itself stays the
stub backend; the agent does not need to do real work.

Env (set by the deploy):
  AGENT_TYPE   — persona label (finops / auditor / rogue); also the default tool intent
  GW_URL       — the gateway MCP endpoint (JSON-RPC over HTTPS)
  AWS_REGION   — signing region (provided by the runtime)
  GW_TOOL      — default tool to call (optional; else inferred from AGENT_TYPE)
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080
_SIGV4_SERVICE = "bedrock-agentcore"

# Default tool intent per persona — drives the same gateway path to an allow
# (FinOps/Auditor) or a Cedar forbid (Rogue, absent from every policy set).
_DEFAULT_TOOL = {
    "finops": ("query_billing", {"columns": ["account_id", "cost_usd", "region"]}),
    "auditor": ("query_dataset", {"dataset": "hr", "table": "payroll"}),
    "rogue": ("query_billing", {"columns": ["tax_id", "customer_email"]}),
}


def _agent_type() -> str:
    return (os.environ.get("AGENT_TYPE") or "finops").lower()


def _qualify(tool: str) -> str:
    """Gateway tools are namespaced ``<target>___<tool>`` (the same form the Cedar
    actions use); qualify a bare tool name so the call resolves and Cedar evaluates."""
    if "___" in tool:
        return tool
    target = os.environ.get("GW_TARGET", "galaxy-tools")
    return f"{target}___{tool}"


def _default_call(agent_type: str):
    override = os.environ.get("GW_TOOL")
    tool, args = _DEFAULT_TOOL.get(agent_type, _DEFAULT_TOOL["finops"])
    return _qualify(override or tool), args


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"


def _get_credentials():
    """Resolve the Runtime's execution-role credentials with the stdlib only
    (the managed code runtime does not ship botocore). Covers the standard env
    vars and the ECS/container credential provider AgentCore injects."""
    ak = os.environ.get("AWS_ACCESS_KEY_ID")
    sk = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        return ak, sk, os.environ.get("AWS_SESSION_TOKEN")
    rel = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    url = ("http://169.254.170.2" + rel) if rel else os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if url:
        req = urllib.request.Request(url)
        auth = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
        tok_file = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
        if tok_file:
            try:
                with open(tok_file) as fh:
                    auth = fh.read().strip()
            except Exception:
                pass
        if auth:
            req.add_header("Authorization", auth)
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read().decode())
        return j["AccessKeyId"], j["SecretAccessKey"], j.get("Token")
    # IMDSv2 — AgentCore Runtime serves the execution role via instance metadata.
    return _imds_credentials()


def _imds_credentials():
    base = "http://169.254.169.254/latest"
    try:
        tok = urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}), timeout=3).read().decode()
        h = {"X-aws-ec2-metadata-token": tok}
        role = urllib.request.urlopen(urllib.request.Request(
            f"{base}/meta-data/iam/security-credentials/", headers=h), timeout=3).read().decode().strip()
        j = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{base}/meta-data/iam/security-credentials/{role}", headers=h), timeout=3).read().decode())
        return j["AccessKeyId"], j["SecretAccessKey"], j.get("Token")
    except Exception:
        return None, None, None


def _sigv4_headers(url: str, body: bytes, region: str):
    """Build SigV4 Authorization headers for a POST, stdlib only. Returns the
    header dict to attach, or raises if no credentials are resolvable."""
    ak, sk, token = _get_credentials()
    if not ak or not sk:
        raise RuntimeError("no credentials in runtime")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    path = parsed.path or "/"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    headers = {
        "content-type": "application/json",
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if token:
        headers["x-amz-security-token"] = token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join([
        "POST", urllib.parse.quote(path, safe="/-_.~"), "",
        canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{_SIGV4_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + sk).encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, _SIGV4_SERVICE)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers["accept"] = "application/json, text/event-stream"
    return headers


def call_gateway(method: str, params: dict) -> dict:
    """Issue one SigV4-signed MCP JSON-RPC call to the governance gateway and
    return a structured result. Never raises — a denial (Cedar forbid / interceptor
    block) comes back as an HTTP 4xx from the gateway and is reported, not thrown,
    so the boundary decision is observable."""
    gw_url = os.environ.get("GW_URL")
    if not gw_url:
        return {"ok": False, "error": "GW_URL not configured"}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    try:
        headers = _sigv4_headers(gw_url, body, _region())
        http_req = urllib.request.Request(gw_url, data=body, method="POST")
        for k, v in headers.items():
            http_req.add_header(k, v)
        with urllib.request.urlopen(http_req, timeout=20) as resp:
            raw = resp.read().decode()
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                payload = raw[:800]  # MCP may stream text/event-stream
        return {"ok": True, "status": resp.status, "gateway_response": payload}
    except Exception as e:  # urllib raises HTTPError for 4xx/5xx — capture the boundary verdict
        status = getattr(e, "code", None)
        try:
            detail = e.read().decode()[:800]  # type: ignore[attr-defined]
        except Exception:
            detail = str(e)[:800]
        # A Cedar forbid / interceptor block is the expected, security-relevant outcome.
        return {"ok": False, "status": status, "gateway_response": detail}


# ── OpenTelemetry GenAI spans → CloudWatch (AgentCore observability) ────────────
# The runtime presets OTEL_PYTHON_DISTRO=aws_distro / aws_configurator and the
# CloudWatch log/metric headers. With aws-opentelemetry-distro installed (see the
# code artifact's requirements.txt), the spans emitted here export to CloudWatch
# GenAI Observability. Import is lazy so the agent runs even where OTel is absent.
_OTEL_READY = None


def _ensure_otel() -> bool:
    global _OTEL_READY
    if _OTEL_READY is not None:
        return _OTEL_READY
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        if not isinstance(trace.get_tracer_provider(), TracerProvider):
            # Not already wired by opentelemetry-instrument — run the configured
            # distro (OTEL_PYTHON_DISTRO=aws_distro) ourselves so the AWS exporters
            # are installed from the preset env.
            try:
                from opentelemetry.instrumentation.auto_instrumentation import initialize
                initialize()
            except Exception:
                pass
        _OTEL_READY = isinstance(trace.get_tracer_provider(), TracerProvider)
    except Exception:
        _OTEL_READY = False
    return _OTEL_READY


def _get_tracer():
    if not _ensure_otel():
        return None
    from opentelemetry import trace
    return trace.get_tracer("galaxy.agentcore.runtime")


def _gateway_turn(agent_type: str, payload: dict, method: str) -> dict:
    if method == "tools/list":
        return call_gateway("tools/list", {})
    tool, args = _default_call(agent_type)
    tool = _qualify(payload["tool"]) if payload.get("tool") else tool
    args = payload.get("arguments") if payload.get("arguments") is not None else args
    result = call_gateway("tools/call", {"name": tool, "arguments": args})
    result["tool"] = tool
    return result


def _traced_turn(agent_type: str, payload: dict, method: str) -> dict:
    """Run the turn inside a GenAI span so the agent shows up in CloudWatch GenAI
    Observability (sessions / traces), tagged with the governance decision."""
    tracer = _get_tracer()
    if tracer is None:
        return _gateway_turn(agent_type, payload, method)
    with tracer.start_as_current_span(f"invoke_agent {agent_type}") as span:
        span.set_attribute("gen_ai.system", "aws.bedrock_agentcore")
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", agent_type)
        sid = payload.get("session_id") or os.environ.get("RUNTIME_SESSION_ID")
        if sid:
            span.set_attribute("session.id", sid)
        result = _gateway_turn(agent_type, payload, method)
        decision = _classify(result)
        span.set_attribute("galaxy.governance.decision", decision)
        if result.get("tool"):
            span.set_attribute("gen_ai.tool.name", result["tool"])
        span.add_event("governance.decision", {"decision": decision, "method": method})
        return result


def handle_turn(payload: dict) -> dict:
    """Run one turn. Optional payload keys: ``method`` (default ``tools/call``),
    ``tool``, ``arguments``, ``prompt`` (recorded for trace context)."""
    agent_type = _agent_type()
    method = payload.get("method") or "tools/call"
    if method == "debug/env":
        # All key NAMES (never secret values) + the non-secret OTEL/collector values,
        # to discover the observability collector the runtime exposes.
        keys = sorted(k for k in os.environ if "SECRET" not in k.upper() and "KEY" not in k.upper())
        otel = {k: os.environ[k] for k in os.environ
                if k.upper().startswith(("OTEL_", "OTLP", "AWS_OTEL", "AGENT_OBS"))}
        imds = {}
        try:  # IMDSv2 probe — does the runtime serve the execution role via metadata?
            tok_req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                             headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
            tok = urllib.request.urlopen(tok_req, timeout=3).read().decode()
            h = {"X-aws-ec2-metadata-token": tok}
            role = urllib.request.urlopen(urllib.request.Request(
                "http://169.254.169.254/latest/meta-data/iam/security-credentials/", headers=h),
                timeout=3).read().decode()
            imds = {"reachable": True, "role": role[:80]}
        except Exception as e:
            imds = {"reachable": False, "err": str(e)[:120]}
        otel_diag = {"ready": _ensure_otel()}
        try:
            import opentelemetry  # noqa: F401
            otel_diag["api"] = True
            from opentelemetry import trace
            otel_diag["provider"] = type(trace.get_tracer_provider()).__name__
        except Exception as e:
            otel_diag["api"] = False
            otel_diag["err"] = str(e)[:120]
        try:
            import amazon.opentelemetry.distro  # noqa: F401
            otel_diag["aws_distro"] = True
        except Exception:
            otel_diag["aws_distro"] = False
        import platform
        import sys
        plat = {"machine": platform.machine(), "python": sys.version.split()[0],
                "platform": sys.platform, "sys_path_tail": sys.path[-3:]}
        return {"agent_type": agent_type, "env_keys": keys, "otel_env": otel,
                "otel_diag": otel_diag, "platform": plat,
                "agentcore_runtime_url": os.environ.get("AGENTCORE_RUNTIME_URL"),
                "aws_execution_env": os.environ.get("AWS_EXECUTION_ENV"),
                "imds": imds}
    result = _traced_turn(agent_type, payload, method)
    return {
        "agent_type": agent_type,
        "prompt": payload.get("prompt"),
        "gateway": result,
        "decision": _classify(result),
        "otel": _OTEL_READY,
    }


def _classify(result: dict) -> str:
    """Map the gateway outcome to a governance verdict. MCP returns a Cedar/
    interceptor denial as HTTP 200 with a JSON-RPC ``error`` (or ``result.isError``),
    so inspect the body, not just the HTTP status."""
    gr = result.get("gateway_response")
    if isinstance(gr, dict):
        if "error" in gr:
            return "denied"
        res = gr.get("result")
        if isinstance(res, dict) and res.get("isError"):
            return "denied"
    if result.get("ok"):
        return "allowed"
    return "blocked_or_denied"  # transport/4xx (e.g. SigV4 or gateway-level block)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — http.server contract
        if self.path.rstrip("/") == "/ping":
            self._send(200, {"status": "healthy", "agent_type": _agent_type()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 — http.server contract
        if self.path.rstrip("/") != "/invocations":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode() or "{}")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        self._send(200, handle_turn(payload if isinstance(payload, dict) else {}))

    def log_message(self, *args):  # quiet the default stderr access log
        return


def serve(port: int = PORT) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"runtime_agent: serving {_agent_type()} on :{port}", flush=True)
    server.serve_forever()


def handler(event: Any = None, context: Any = None) -> dict:
    """Handler-style entry point. AgentCore's code-deploy ``entryPoint`` may invoke
    a callable rather than launch the HTTP server; this satisfies that contract by
    delegating to the same ``handle_turn``. ``event`` may be the payload dict, a
    JSON string, or carry a ``payload``/``body`` field."""
    payload: Any = event
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            payload = {"prompt": payload}
    if isinstance(payload, dict) and not {"method", "tool", "arguments", "prompt"} & set(payload):
        payload = payload.get("payload") or payload.get("body") or payload
        if isinstance(payload, (bytes, str)):
            try:
                payload = json.loads(payload or "{}")
            except Exception:
                payload = {}
    return handle_turn(payload if isinstance(payload, dict) else {})


if __name__ == "__main__":
    serve()
