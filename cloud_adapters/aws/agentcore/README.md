# Deploying the governance integration on AgentCore

This wires the framework's enforcement into Amazon Bedrock AgentCore. The split
follows `docs/agentcore-comparison.md`: AgentCore Policy (Cedar) handles coarse
authorization; the governance **interceptors** add the content controls Cedar
cannot express; both read the one NHI-keyed registry.

The interceptor and identity code here is verified offline (`tests/test_agentcore.py`).
The Cedar + Policy/Identity/Gateway provisioning is also **verified live** on
us-east-2 (acct 774435790385) and codified in `scripts/deploy_agentcore.py`
(idempotent; `--teardown` removes everything). AgentCore's Terraform surface is
still maturing, so provisioning uses the control-plane API (boto3) rather than
fabricated `aws_bedrockagentcore_*` resources.

## One-command deploy / teardown

```bash
# 1. Build the content-control interceptor zip (no Docker; Linux wheels via uv):
scripts/build_interceptor_zip.sh            # → .build/interceptor.zip

# 2. Provision everything (idempotent):
AWS_PROFILE=<profile> PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2
AWS_PROFILE=<profile> PYTHONPATH=. python scripts/deploy_agentcore.py --region us-east-2 --teardown
```

It provisions, in order: two IAM roles → a stub tool Lambda → an MCP Gateway
(AWS_IAM auth) + a Lambda target exposing the tools → a Policy engine + one Cedar
policy per (agent, tool) from `cedar_export` → a workload identity per agent →
the **content-control interceptor Lambdas** (`galaxy-gov-request` /
`galaxy-gov-response`, from `.build/interceptor.zip`) → and binds the policy engine
(**ENFORCE**) + attaches the interceptors to the gateway. Live result: gateway
`galaxy-governance-gw` with Cedar authz **and** content interceptors, policy engine
`galaxy_governance` (9 policies), identities `galaxy_{finops,auditor,rogue}`.

Why zip and not a container image: the ECR push path is blocked by an org SCP in
this account (`ecr:UploadLayerPart` denied), so the interceptors ship as zip
Lambdas with Linux-platform wheels. Where ECR is available, the `Dockerfile` here
is the container alternative.

The manual steps below document what the script does, for reference.

## Artifacts produced from the registry

```bash
# Coarse-authz Cedar policies (source of truth = the policy registry):
python -m governance.agentcore.cedar_export > agentcore.cedar

# The resolved policy registry the interceptors read:
python -m governance.policy_export > cloud_adapters/aws/infra/lambda/agent-controls.json
```

## Components

| AgentCore primitive | What we attach | Code |
|---|---|---|
| Policy engine (Cedar) | generated `agentcore.cedar` | `governance/agentcore/cedar_export.py` |
| Gateway request interceptor (Lambda) | input guards + tool-plan content checks | `request_interceptor.py` |
| Gateway response interceptor (Lambda) | output redaction + tool-list filtering | `response_interceptor.py` |
| Identity | NHI → AgentCore Identity | `identity.py` |
| Observability | OTel spans + hash-chained ledger (existing) | `governance/adapters` |

## Steps

1. **Build the interceptor image** (shared with the chokepoint Lambdas):
   `docker build -f cloud_adapters/aws/infra/lambda/Dockerfile -t <ecr>/galaxy-gov-proxy .`
   then push to ECR. The image bundles `governance/{shared,remote}` + the toolkit and
   bakes `agent-controls.json` at `/var/task`.

2. **Create the interceptor Lambdas** from that image, overriding the handler:
   - request: `image_config.command = ["request_interceptor.handler"]`
   - response: `image_config.command = ["response_interceptor.handler"]`
   (Both read `GOV_POLICY_REGISTRY_PATH=/var/task/agent-controls.json`.)

3. **Create the Gateway** and attach the interceptors as its request/response
   interceptors (AgentCore Gateway → interceptor configuration).

4. **Create a policy engine**, load `agentcore.cedar`, and associate it with the
   Gateway so Cedar authz runs on every tool call before execution.

5. **Configure Identity**: register each agent's workload identity and set
   `AGENTCORE_IDENTITY_<AGENT_TYPE>` (or the portable `NHI_CLIENT_ID_<AGENT_TYPE>`),
   consumed by `AgentCoreIdentityProvider`.

6. **State** for stateful controls (drift/circuit/cost/rate): provision a DynamoDB
   table and point `governance.shared.state.DynamoDbState` at it.

## Division of labor (avoid double-enforcement)

- **Cedar / AgentCore Policy**: which tool, which recipient, simple input-param conditions.
- **Interceptors (this dir)**: prompt-injection, credential/PII redaction, context-budget,
  blocked-pattern, output content-safety, tool-list filtering.
- **Data FGAC**: the data-access proxy (`../infra/lambda/data_proxy.py`), fronted by Gateway.

The agent runtime stays untrusted: every control is resolved from the registry,
not from the request.
