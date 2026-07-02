"""
cloud_adapters/azure/infra/functions/function_app.py — Azure Functions app (v2 model).

The transport shell for the Azure "Method 1" out-of-process chokepoints. It maps
three HTTP routes onto the pure decision functions (which carry all the
governance logic and are unit-tested without the Functions runtime):

  POST /api/llm    → llm_proxy.enforce_llm    (APIM → Azure OpenAI egress)
  POST /api/data   → data_proxy.enforce_data  (FGAC data-access chokepoint)
  POST /api/a2a    → a2a_broker.enforce_a2a    (agent-to-agent authorization)

Auth level is ``FUNCTION`` (host key) as defence-in-depth; in the reference
topology these functions sit behind API Management, which validates the
subscription key and required headers before the request reaches here.

``azure.functions`` is imported at module load (the Functions host requires it),
so this module is only imported inside the deployed Function App — never by the
test suite, which imports the ``*_proxy`` / ``a2a_broker`` modules directly.
"""

from __future__ import annotations

import json

import azure.functions as func

from cloud_adapters.azure.infra.functions.a2a_broker import enforce_a2a
from cloud_adapters.azure.infra.functions.data_proxy import enforce_data
from cloud_adapters.azure.infra.functions.llm_proxy import enforce_llm

app = func.FunctionApp()


def _dispatch(req: "func.HttpRequest", fn) -> "func.HttpResponse":
    try:
        body = req.get_json() if req.get_body() else {}
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "invalid JSON body"}), status_code=400, mimetype="application/json"
        )
    status, payload = fn(body, dict(req.headers))
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype="application/json")


@app.route(route="llm", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def llm(req: "func.HttpRequest") -> "func.HttpResponse":
    return _dispatch(req, enforce_llm)


@app.route(route="data", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def data(req: "func.HttpRequest") -> "func.HttpResponse":
    return _dispatch(req, enforce_data)


@app.route(route="a2a", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def a2a(req: "func.HttpRequest") -> "func.HttpResponse":
    return _dispatch(req, enforce_a2a)
