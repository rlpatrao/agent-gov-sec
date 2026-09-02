"""
scripts/deploy_agentcore.py — provision (and tear down) the governance→AgentCore deploy.

Creates the full, reversible AgentCore footprint that realizes the framework's
coarse-authz + identity layers from the policy registry, verified live on
us-east-2 (acct <ACCOUNT_ID>):

  IAM role (gateway exec) + IAM role (tool lambda)
  → stub tool Lambda (galaxy-tools)
  → MCP Gateway (AWS_IAM auth) + Lambda target exposing the tools
  → Policy engine + one Cedar policy per (agent, tool) from cedar_export
  → workload identity per agent (NHI → AgentCore Identity)
  → associate the policy engine with the gateway in ENFORCE mode
  → per-persona AgentCore Runtime (galaxy_{finops,auditor,rogue}) from an S3
    code artifact, each under its galaxy-rp-<type> execution role so the gateway
    sees the Cedar principal — the personas become observable Runtimes that call
    the governed gateway under their own identity (security enforced per agent).

The Runtime layer uses the S3 `codeConfiguration` path (not a container image)
because the ECR push path is blocked by an org SCP in this account; the hosted
agent is `cloud_adapters/aws/agentcore/runtime_agent.py`. The stub tool backend is
kept — the focus is observability and per-agent security, not agent function.

Idempotent by name; `--teardown` removes everything it created.

Usage:
  AWS_PROFILE=<your-aws-profile> PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2
  AWS_PROFILE=<your-aws-profile> PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2 --dry-run
  AWS_PROFILE=<your-aws-profile> PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2 --invoke rogue
  AWS_PROFILE=<your-aws-profile> PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2 --teardown
"""

from __future__ import annotations

import argparse
import io
import json
import time
import zipfile

import boto3

from galaxy_gov.agentcore.cedar_export import iter_agentcore_policies
from galaxy_gov.policy_export import KNOWN_AGENT_TYPES

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

# Per-persona AgentCore Runtimes (so finops/auditor/rogue are first-class, observable
# runtimes in the console, each calling the gateway under its own identity).
# AGENT_ROLE_PREFIX MUST match galaxy_gov.agentcore.cedar_export.principal_arn so the
# assumed-role principal the gateway sees matches the deployed Cedar policies.
AGENT_ROLE_PREFIX = "galaxy-rp-"
RUNTIME_CODE_KEY = "runtime/agent.zip"
RUNTIME_RUNTIME = "PYTHON_3_12"
# entryPoint for the S3 code-deploy path: PYTHON_3_x requires the .py file itself
# (AgentCore launches it; runtime_agent.py serves the :8080 HTTP contract under
# `if __name__ == "__main__"`). A `module.handler` reference is rejected.
RUNTIME_ENTRYPOINT = ["runtime_agent.py"]

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


def deploy(region, dry_run=False, with_runtimes=True):
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
        gw_id, gw_arn, gw_url = r["gatewayId"], r["gatewayArn"], r.get("gatewayUrl")
        print(f"gateway: created {gw_id}")
        while c.get_gateway(gatewayIdentifier=gw_id)["status"] == "CREATING":
            time.sleep(3)
    else:
        g = c.get_gateway(gatewayIdentifier=gw_id)  # list items omit the ARN/URL
        gw_arn, gw_url = g["gatewayArn"], g.get("gatewayUrl")
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
    print(f"\nGateway {gw_id} ENFORCE-bound to policy engine {engine_id}"
          f"{' + content interceptors' if interceptors else ''} in {region}.")

    if with_runtimes:
        _deploy_runtimes(c, iam, region, account, gw_url, dry_run=dry_run)
    print(f"\nDone ({region}).")


# ── Per-persona AgentCore Runtimes ─────────────────────────────────────────────

def _agent_role_arn(account, agent_type):
    return f"arn:aws:iam::{account}:role/{AGENT_ROLE_PREFIX}{agent_type.lower()}"


def _ensure_agent_role(iam, account, agent_type):
    """Per-persona execution role named to match the Cedar principal
    (`assumed-role/galaxy-rp-<type>`). Trusts bedrock-agentcore (and keeps
    ecs-tasks so a Terraform-owned role of the same name is not broken)."""
    name = f"{AGENT_ROLE_PREFIX}{agent_type.lower()}"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
             "Principal": {"Service": ["bedrock-agentcore.amazonaws.com", "ecs-tasks.amazonaws.com"]},
             "Action": "sts:AssumeRole"}]}
    try:
        iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
        print(f"agent-role: created {name}")
    except Exception as e:
        if _exists(e):
            try:  # ensure bedrock-agentcore is trusted even if the role predated this
                iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
                print(f"agent-role: trust ensured {name}")
            except Exception:
                print(f"agent-role: exists {name}")
        else:
            print(f"agent-role: FAILED {name} → {str(e)[:120]}")
    iam.put_role_policy(RoleName=name, PolicyName="galaxy-rp-runtime-perms", PolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["bedrock-agentcore:*"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], "Resource": "*"}]}))
    return _agent_role_arn(account, agent_type)


def _ensure_runtime_bucket(s3, account, region):
    bucket = f"galaxy-agentcore-runtime-{account}"
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
        print(f"s3: created {bucket}")
    except Exception as e:
        if "BucketAlreadyOwnedByYou" in type(e).__name__ or _exists(e):
            print(f"s3: exists {bucket}")
        else:
            print(f"s3: note {bucket} → {str(e)[:100]}")
    return bucket


def _runtime_zip_path():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/.build/runtime.zip"


def _upload_runtime_code(s3, bucket):
    import os
    vendored = _runtime_zip_path()
    if os.path.exists(vendored):
        # Prebuilt artifact with aws-opentelemetry-distro vendored for GenAI
        # observability (scripts/build_runtime_zip.sh). The bare runtime installs no
        # requirements.txt, so ADOT must be in the zip for spans to reach CloudWatch.
        with open(vendored, "rb") as fh:
            blob = fh.read()
        note = "vendored ADOT"
    else:
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "cloud_adapters", "aws", "agentcore", "runtime_agent.py")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.write(src, "runtime_agent.py")
        blob = buf.getvalue()
        note = "stdlib-only (no GenAI spans — run scripts/build_runtime_zip.sh first)"
    s3.put_object(Bucket=bucket, Key=RUNTIME_CODE_KEY, Body=blob)
    print(f"s3: uploaded s3://{bucket}/{RUNTIME_CODE_KEY} ({len(blob)} bytes — {note})")
    return RUNTIME_CODE_KEY


def _runtime_params(name, at, role_arn, bucket, key, gw_url):
    return dict(
        agentRuntimeName=name,
        description=f"Galaxy governed persona {at} — observability + per-agent security demo",
        roleArn=role_arn,
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": bucket, "prefix": key}},
            "runtime": RUNTIME_RUNTIME, "entryPoint": RUNTIME_ENTRYPOINT}},
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables={"AGENT_TYPE": at.lower(), "GW_URL": gw_url or "", "GW_TARGET": TARGET_NAME},
    )


def _deploy_runtimes(c, iam, region, account, gw_url, dry_run=False):
    s3 = boto3.client("s3", region_name=region)
    if dry_run:
        for at in KNOWN_AGENT_TYPES:
            name = f"galaxy_{at.lower()}"
            p = _runtime_params(name, at, _agent_role_arn(account, at),
                                f"galaxy-agentcore-runtime-{account}", RUNTIME_CODE_KEY, gw_url)
            print(f"\n[dry-run] create_agent_runtime {name}:\n" + json.dumps(p, indent=2, default=str))
        return
    bucket = _ensure_runtime_bucket(s3, account, region)
    key = _upload_runtime_code(s3, bucket)
    existing = {r["agentRuntimeName"]: r for r in _list(c.list_agent_runtimes, "agentRuntimes")}
    for at in KNOWN_AGENT_TYPES:
        name = f"galaxy_{at.lower()}"
        role_arn = _ensure_agent_role(iam, account, at)
        p = _runtime_params(name, at, role_arn, bucket, key, gw_url)
        try:
            if name in existing:
                c.update_agent_runtime(agentRuntimeId=existing[name]["agentRuntimeId"],
                                       agentRuntimeArtifact=p["agentRuntimeArtifact"], roleArn=role_arn,
                                       networkConfiguration=p["networkConfiguration"],
                                       protocolConfiguration=p["protocolConfiguration"],
                                       environmentVariables=p["environmentVariables"])
                print(f"runtime: updated {name}")
            else:
                time.sleep(8)  # role propagation before the runtime assumes it
                r = c.create_agent_runtime(**p)
                print(f"runtime: created {name} → {r['agentRuntimeArn']}")
        except Exception as e:
            print(f"runtime: {'update' if name in existing else 'create'} FAILED {name} → {str(e)[:200]}")


def _invoke_runtime(ctl, rt, agent_type, prompt, method):
    """Invoke one persona Runtime; return (statusCode, session_id, parsed_body)."""
    name = f"galaxy_{agent_type.lower()}"
    arn = next((r["agentRuntimeArn"] for r in _list(ctl.list_agent_runtimes, "agentRuntimes")
                if r["agentRuntimeName"] == name), None)
    if not arn:
        return None, None, {"error": f"no runtime {name} (deploy first)"}
    payload = json.dumps({"prompt": prompt or f"{agent_type} demo turn", "method": method}).encode()
    resp = rt.invoke_agent_runtime(agentRuntimeArn=arn, payload=payload,
                                   contentType="application/json", accept="application/json")
    body = resp.get("response")
    if hasattr(body, "read"):
        body = body.read()
    out = body.decode() if isinstance(body, (bytes, bytearray)) else body
    try:
        parsed = json.loads(out)
    except Exception:
        parsed = {"raw": out}
    return resp.get("statusCode"), resp.get("runtimeSessionId"), parsed


def invoke(region, agent_type, prompt=None, method="tools/call"):
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    rt = boto3.client("bedrock-agentcore", region_name=region)
    if agent_type.lower() == "all":
        return invoke_all(region, method, prompt, ctl=ctl, rt=rt)
    code, sid, parsed = _invoke_runtime(ctl, rt, agent_type, prompt, method)
    print(f"galaxy_{agent_type.lower()} (HTTP {code}, session {sid}):\n{json.dumps(parsed)}")


def invoke_all(region, method="tools/call", prompt=None, ctl=None, rt=None):
    """Drive every persona Runtime through the governed gateway in one run (the
    cloud analogue of demo_agents) and print a per-agent governed-decision table."""
    ctl = ctl or boto3.client("bedrock-agentcore-control", region_name=region)
    rt = rt or boto3.client("bedrock-agentcore", region_name=region)
    print(f"AgentCore Runtime demo — {', '.join(KNOWN_AGENT_TYPES)}  (method={method}, region={region})")
    print(f"  each persona invoked as its own Runtime → governed gateway under its galaxy-rp-<type> identity\n")
    rows = []
    for at in KNOWN_AGENT_TYPES:
        code, sid, parsed = _invoke_runtime(ctl, rt, at, prompt, method)
        gw = parsed.get("gateway", {}) if isinstance(parsed, dict) else {}
        decision = parsed.get("decision", "?") if isinstance(parsed, dict) else "?"
        gr = gw.get("gateway_response")
        if method == "tools/list" and isinstance(gr, dict):
            tools = [t.get("name") for t in (gr.get("result", {}) or {}).get("tools", [])]
            detail = f"tools: {tools}" if tools else "tools: [] (Cedar-filtered)"
        elif isinstance(gr, dict) and "error" in gr:
            detail = gr["error"].get("message", "")[:70]
        else:
            detail = "stub tool executed"
        rows.append((at, decision, gw.get("tool", ""), detail, parsed.get("otel")))
        print(f"  {at:8} → {decision:8} | otel={parsed.get('otel')} | {gw.get('tool','') or method}")
        print(f"             {detail}")
    allowed = sum(1 for r in rows if r[1] == "allowed")
    denied = sum(1 for r in rows if r[1] == "denied")
    print(f"\n  summary: {allowed} allowed · {denied} denied · {len(rows)} agents "
          f"(spans → CloudWatch GenAI Observability)")


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
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

    # per-persona runtimes + their execution roles + the code bucket
    for r in _list(c.list_agent_runtimes, "agentRuntimes"):
        if r.get("agentRuntimeName", "").startswith("galaxy_"):
            try:
                c.delete_agent_runtime(agentRuntimeId=r["agentRuntimeId"])
                print(f"deleted runtime {r['agentRuntimeName']}")
            except Exception:
                pass
    for at in KNOWN_AGENT_TYPES:
        rn = f"{AGENT_ROLE_PREFIX}{at.lower()}"
        try:
            iam.delete_role_policy(RoleName=rn, PolicyName="galaxy-rp-runtime-perms")
            iam.delete_role(RoleName=rn)
            print(f"deleted role {rn}")
        except Exception:
            pass
    try:
        s3 = boto3.client("s3", region_name=region)
        bucket = f"galaxy-agentcore-runtime-{account}"
        s3.delete_object(Bucket=bucket, Key=RUNTIME_CODE_KEY)
        s3.delete_bucket(Bucket=bucket)
        print(f"deleted bucket {bucket}")
    except Exception:
        pass

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
    ap.add_argument("--dry-run", action="store_true",
                    help="print the create_agent_runtime calls without provisioning")
    ap.add_argument("--no-runtimes", action="store_true",
                    help="provision only the gateway/policy/identity layer (skip the per-persona Runtimes)")
    ap.add_argument("--invoke", metavar="AGENT",
                    help="invoke a deployed persona Runtime (finops/auditor/rogue, or 'all' for the "
                         "demo-style run across every persona) and print its governed result")
    ap.add_argument("--prompt", help="prompt for --invoke")
    ap.add_argument("--method", default="tools/call",
                    help="MCP method for --invoke (tools/call | tools/list | debug/env)")
    args = ap.parse_args()
    if args.invoke:
        invoke(args.region, args.invoke, args.prompt, args.method)
    elif args.teardown:
        teardown(args.region)
    else:
        deploy(args.region, dry_run=args.dry_run, with_runtimes=not args.no_runtimes)
