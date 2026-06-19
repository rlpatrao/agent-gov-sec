"""
scripts/deploy_agentcore.py — provision (and tear down) the governance→AgentCore deploy.

Creates the full, reversible AgentCore footprint that realizes the framework's
coarse-authz + identity layers from the policy registry, verified live on
us-east-2 (acct 774435790385):

  IAM role (gateway exec) + IAM role (tool lambda)
  → stub tool Lambda (galaxy-tools)
  → MCP Gateway (AWS_IAM auth) + Lambda target exposing the tools
  → Policy engine + one Cedar policy per (agent, tool) from cedar_export
  → workload identity per agent (NHI → AgentCore Identity)
  → associate the policy engine with the gateway in ENFORCE mode

Idempotent by name; `--teardown` removes everything it created.

Usage:
  AWS_PROFILE=SuperAdmin-774435790385 PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2
  AWS_PROFILE=SuperAdmin-774435790385 PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2 --teardown
"""

from __future__ import annotations

import argparse
import io
import json
import time
import zipfile

import boto3

from governance.agentcore.cedar_export import iter_agentcore_policies
from governance.policy_export import KNOWN_AGENT_TYPES

ENGINE_NAME = "galaxy_governance"
GW_NAME = "galaxy-governance-gw"
GW_ROLE = "galaxy-agentcore-gateway"
LAMBDA_ROLE = "galaxy-tool-lambda"
LAMBDA_FN = "galaxy-tools"
TARGET_NAME = "galaxy-tools"
GW_TOOLS = ["query_billing", "summarize_costs", "query_dataset"]

# Content-control interceptors (the rewritten model-boundary controls). Deployed
# as zip Lambdas (the container/ECR path is blocked by SCP in some org accounts);
# build the zip with scripts/build_interceptor_zip.sh.
INTERCEPTOR_REQ = "galaxy-gov-request"
INTERCEPTOR_RESP = "galaxy-gov-response"
INTERCEPTOR_ZIP = "/.build/interceptor.zip"  # relative to repo root, see below

_TOOL_SCHEMA = [
    {"name": "query_billing", "description": "Read finops billing columns",
     "inputSchema": {"type": "object", "properties": {"columns": {"type": "array", "items": {"type": "string"}}}, "required": ["columns"]}},
    {"name": "summarize_costs", "description": "Summarize fetched cost data",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "query_dataset", "description": "Cross-dataset read (auditor)",
     "inputSchema": {"type": "object", "properties": {"dataset": {"type": "string"}, "table": {"type": "string"}}, "required": ["dataset", "table"]}},
]


def _list(fn, key, **kw):
    items, token = [], None
    while True:
        resp = fn(**({**kw, "nextToken": token} if token else kw))
        items += resp.get(key) or resp.get("items") or []
        token = resp.get("nextToken")
        if not token:
            return items


def _exists(e):
    return "already exists" in str(e) or "EntityAlreadyExists" in type(e).__name__ or "ConflictException" in type(e).__name__


# ── IAM + Lambda backend ──────────────────────────────────────────────────────

def _ensure_roles(iam, account):
    gw_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    lam_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                 "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    for name, trust in ((GW_ROLE, gw_trust), (LAMBDA_ROLE, lam_trust)):
        try:
            iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
            print(f"iam-role: created {name}")
        except Exception as e:
            print(f"iam-role: {'exists' if _exists(e) else 'FAILED'} {name}")
    iam.put_role_policy(RoleName=GW_ROLE, PolicyName="galaxy-gw-perms", PolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": f"arn:aws:lambda:*:{account}:function:galaxy-*"},
            {"Effect": "Allow", "Action": ["bedrock-agentcore:*"], "Resource": "*"}]}))
    try:
        iam.attach_role_policy(RoleName=LAMBDA_ROLE,
                               PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    except Exception:
        pass
    return f"arn:aws:iam::{account}:role/{GW_ROLE}", f"arn:aws:iam::{account}:role/{LAMBDA_ROLE}"


def _ensure_lambda(lam, region, account, lam_role_arn):
    src = "import json\ndef handler(event, context):\n    return {'statusCode': 200, 'body': json.dumps({'result': 'stub governed tool response'})}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("handler.py", src)
    try:
        time.sleep(8)  # role propagation
        lam.create_function(FunctionName=LAMBDA_FN, Runtime="python3.12", Role=lam_role_arn,
                            Handler="handler.handler", Code={"ZipFile": buf.getvalue()})
        print(f"lambda: created {LAMBDA_FN}")
    except Exception as e:
        print(f"lambda: {'exists' if _exists(e) else 'FAILED → ' + str(e)[:120]} {LAMBDA_FN}")
    return f"arn:aws:lambda:{region}:{account}:function:{LAMBDA_FN}"


# ── Gateway + target + engine + policies + identities ──────────────────────────

def _find_gateway(c):
    for g in _list(c.list_gateways, "items"):
        if g.get("name") == GW_NAME:
            return g.get("gatewayId"), g.get("gatewayArn")
    return None, None


def _find_engine(c):
    for e in _list(c.list_policy_engines, "policyEngines"):
        if e.get("name") == ENGINE_NAME:
            return e.get("policyEngineId") or e.get("id")
    return None


def deploy(region):
    sts = boto3.client("sts", region_name=region)
    account = sts.get_caller_identity()["Account"]
    iam = boto3.client("iam")
    c = boto3.client("bedrock-agentcore-control", region_name=region)
    lam = boto3.client("lambda", region_name=region)

    gw_role_arn, lam_role_arn = _ensure_roles(iam, account)
    lambda_arn = _ensure_lambda(lam, region, account, lam_role_arn)

    gw_id, gw_arn = _find_gateway(c)
    if not gw_id:
        time.sleep(8)  # role propagation for the gateway trust
        r = c.create_gateway(name=GW_NAME, roleArn=gw_role_arn, protocolType="MCP", authorizerType="AWS_IAM")
        gw_id, gw_arn = r["gatewayId"], r["gatewayArn"]
        print(f"gateway: created {gw_id}")
        while c.get_gateway(gatewayIdentifier=gw_id)["status"] == "CREATING":
            time.sleep(3)
    else:
        gw_arn = c.get_gateway(gatewayIdentifier=gw_id)["gatewayArn"]  # list items omit the ARN
        print(f"gateway: reuse {gw_id}")

    if not _list(c.list_gateway_targets, "items", gatewayIdentifier=gw_id):
        c.create_gateway_target(
            gatewayIdentifier=gw_id, name=TARGET_NAME,
            targetConfiguration={"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": _TOOL_SCHEMA}}}},
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}])
        print(f"target: created {TARGET_NAME}")
    else:
        print(f"target: exists {TARGET_NAME}")

    engine_id = _find_engine(c)
    if not engine_id:
        engine_id = c.create_policy_engine(name=ENGINE_NAME, description="Galaxy governance coarse authz")["policyEngineId"]
        print(f"policy-engine: created {engine_id}")
    else:
        print(f"policy-engine: reuse {engine_id}")

    for name, stmt in iter_agentcore_policies(gateway_arn=gw_arn, target_name=TARGET_NAME,
                                              gateway_tools=GW_TOOLS, account_id=account):
        try:
            c.create_policy(name=name, definition={"cedar": {"statement": stmt}}, policyEngineId=engine_id)
            print(f"  policy: created {name}")
        except Exception as e:
            print(f"  policy: {'exists' if _exists(e) else 'FAILED → ' + str(e)[:120]} {name}")

    for at in KNOWN_AGENT_TYPES:
        try:
            c.create_workload_identity(name=f"galaxy_{at.lower()}")
            print(f"workload-identity: created galaxy_{at.lower()}")
        except Exception as e:
            print(f"workload-identity: {'exists' if _exists(e) else 'FAILED'} galaxy_{at.lower()}")

    interceptors = _deploy_interceptors(lam, region, account, gw_arn)

    engine_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:policy-engine/{engine_id}"
    kwargs = dict(gatewayIdentifier=gw_id, name=GW_NAME, roleArn=gw_role_arn,
                  protocolType="MCP", authorizerType="AWS_IAM",
                  policyEngineConfiguration={"arn": engine_arn, "mode": "ENFORCE"})
    if interceptors:
        kwargs["interceptorConfigurations"] = interceptors
    c.update_gateway(**kwargs)
    print(f"\nDone. Gateway {gw_id} ENFORCE-bound to policy engine {engine_id}"
          f"{' + content interceptors' if interceptors else ''} in {region}.")


def _zip_path():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + INTERCEPTOR_ZIP


def _deploy_interceptors(lam, region, account, gw_arn):
    """Create/update the request+response interceptor Lambdas from the prebuilt
    zip and return the gateway interceptor-configurations. Skips (returns []) if
    the zip is absent — run scripts/build_interceptor_zip.sh first."""
    import os
    zip_path = _zip_path()
    if not os.path.exists(zip_path):
        print(f"interceptors: SKIP (build first: scripts/build_interceptor_zip.sh) — {zip_path} missing")
        return []
    with open(zip_path, "rb") as fh:
        blob = fh.read()
    role = f"arn:aws:iam::{account}:role/{LAMBDA_ROLE}"
    arns = {}
    for fn, handler in ((INTERCEPTOR_REQ, "request_interceptor.handler"),
                        (INTERCEPTOR_RESP, "response_interceptor.handler")):
        try:
            r = lam.create_function(FunctionName=fn, Runtime="python3.12", Role=role, Handler=handler,
                                    Code={"ZipFile": blob}, Timeout=30, MemorySize=512,
                                    Environment={"Variables": {"GOV_POLICY_REGISTRY_PATH": "/var/task/agent-controls.json"}})
            print(f"interceptor: created {fn}")
        except Exception as e:
            if _exists(e):
                lam.update_function_code(FunctionName=fn, ZipFile=blob)
                print(f"interceptor: updated {fn}")
                r = lam.get_function(FunctionName=fn)["Configuration"]
            else:
                print(f"interceptor: FAILED {fn} → {str(e)[:120]}"); return []
        arns[fn] = r.get("FunctionArn") or f"arn:aws:lambda:{region}:{account}:function:{fn}"
        try:
            lam.add_permission(FunctionName=fn, StatementId="agentcore-gw", Action="lambda:InvokeFunction",
                               Principal="bedrock-agentcore.amazonaws.com", SourceArn=gw_arn)
        except Exception:
            pass
    return [
        {"interceptor": {"lambda": {"arn": arns[INTERCEPTOR_REQ]}}, "interceptionPoints": ["REQUEST"],
         "inputConfiguration": {"passRequestHeaders": True}},
        {"interceptor": {"lambda": {"arn": arns[INTERCEPTOR_RESP]}}, "interceptionPoints": ["RESPONSE"],
         "inputConfiguration": {"passRequestHeaders": True}},
    ]


def teardown(region):
    c = boto3.client("bedrock-agentcore-control", region_name=region)
    iam, lam = boto3.client("iam"), boto3.client("lambda", region_name=region)
    gw_id, _ = _find_gateway(c)
    if gw_id:
        for t in _list(c.list_gateway_targets, "items", gatewayIdentifier=gw_id):
            c.delete_gateway_target(gatewayIdentifier=gw_id, targetId=t.get("targetId"))
            print(f"deleted target {t.get('name')}")
        c.delete_gateway(gatewayIdentifier=gw_id)
        print(f"deleted gateway {gw_id}")
    engine_id = _find_engine(c)
    if engine_id:
        for p in _list(c.list_policies, "items", policyEngineId=engine_id):
            c.delete_policy(policyEngineId=engine_id, policyId=p.get("policyId") or p.get("id"))
        c.delete_policy_engine(policyEngineId=engine_id)
        print(f"deleted policy-engine {engine_id}")
    for at in KNOWN_AGENT_TYPES:
        try:
            c.delete_workload_identity(name=f"galaxy_{at.lower()}")
        except Exception:
            pass
    for fn in (LAMBDA_FN, INTERCEPTOR_REQ, INTERCEPTOR_RESP):
        try:
            lam.delete_function(FunctionName=fn)
            print(f"deleted lambda {fn}")
        except Exception:
            pass
    for role, inline in ((GW_ROLE, "galaxy-gw-perms"), (LAMBDA_ROLE, None)):
        try:
            if inline:
                iam.delete_role_policy(RoleName=role, PolicyName=inline)
            else:
                iam.detach_role_policy(RoleName=role, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
            iam.delete_role(RoleName=role)
            print(f"deleted role {role}")
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--teardown", action="store_true")
    args = ap.parse_args()
    (teardown if args.teardown else deploy)(args.region)
