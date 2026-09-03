# Services & technology inventory

**Last updated:** 2026-06-22
**Scope:** This document inventories the external services, Python libraries, governance policy files, and environment variables on which this repository depends. For each item, it records the function performed, the location where it is configured, and its current status. This document covers the AWS stack of the documentation set. Azure and GCP are maintained as separate documentation stacks (`../azure/`, `../gcp/`) and are currently placeholders.

For the system design and sequence diagrams, see [architecture.md](architecture.md).

> **Repo scope.** This repository implements the **Galaxy Agentic Governance Platform**, a runtime governance and security layer (`core/`, `galaxy_gov/`, `core/a2a/`) built on the `agent_os` / `agent_sre` / `agentmesh` packages and the `agent-framework` runtime. It additionally provides a **minimal demonstration payload** (`payload_agents/`) comprising three personas (**FinOps**, **Auditor**, **Rogue**). The full multi-agent migration product (approximately 18 agents, the migration/discovery/scanner pipelines, the `Dockerfile`, and the `legacy/` sample) has been moved to a **local-only, gitignored `archive/`** and is **not part of this repository**. Where this document describes that product, it is labeled **(archived)** for context.

---

## 1. AWS resource topology

This repository supports two AWS enforcement methods. Method 1 routes agents through a Bedrock egress proxy and is provided as reference Terraform under [`cloud_adapters/aws/infra/`](../../cloud_adapters/aws/infra/). Method 2 hosts the agents and the policy enforcement points on Amazon Bedrock AgentCore; it is deployed live on account `<ACCOUNT_ID>`, region `us-east-2`, and is provisioned by [`scripts/deploy_agentcore.py`](../../scripts/deploy_agentcore.py). The two methods share the same governance code paths, with the Cedar and interceptor logic generated from the same policy registry that the offline demo uses. This repository provides the offline governance demo (`scripts/demo_governance.py`), the full demo matrix (`scripts/demo_agents.py`), and the three-persona payload (FinOps, Auditor, Rogue).

### 1.1 Method 1 — Bedrock egress proxy (reference Terraform)

> Provisioned by the Terraform under [`cloud_adapters/aws/infra/`](../../cloud_adapters/aws/infra/). All resources are tagged `galaxy-rp`.

| # | Resource | What it does | Status | Where it touches code |
|---|---|---|---|---|
| 1 | API Gateway (REST `galaxy-rp-bedrock-gw`, `POST /invoke`) | The single egress chokepoint in front of Bedrock. Validates the `x-api-key`; agents reach Bedrock only through this path and never hold Bedrock credentials. | Reference Terraform | [cloud_adapters/aws/gateway.py](../../cloud_adapters/aws/gateway.py); [cloud_adapters/aws/infra/lambda/bedrock_proxy.py](../../cloud_adapters/aws/infra/lambda/bedrock_proxy.py) |
| 2 | Lambda (Bedrock proxy `galaxy-rp-bedrock-proxy`) | Invoked by API Gateway; container image, handler `bedrock_proxy.handler`. SigV4-signs and forwards the request to Bedrock Converse, stamping the attribution headers. Model `us.anthropic.claude-sonnet-4-6` is injected server-side. | Reference Terraform | [cloud_adapters/aws/infra/lambda/bedrock_proxy.py](../../cloud_adapters/aws/infra/lambda/bedrock_proxy.py) |
| 3 | Bedrock Converse | Hosts the model behind the gateway (`AWS_BEDROCK_MODEL_ID`, e.g. `us.anthropic.claude-sonnet-4-6`). | Reference Terraform | [framework_adapters/langgraph/bedrock_gateway.py](../../framework_adapters/langgraph/bedrock_gateway.py) — `BedrockGatewayChatModel` |
| 4 | IAM roles / STS | Per-agent identity. The agent assumes a scoped role; its principal id flows into the relevant `NHI_CLIENT_ID_*` env var. | Reference Terraform | [cloud_adapters/aws/identity.py](../../cloud_adapters/aws/identity.py); [core/nhi_registry.py](../../core/nhi_registry.py) reads `NHI_CLIENT_ID_*` from env |
| 5 | Secrets Manager / SSM Parameter Store | Stores the Bedrock gateway key (`galaxy/bedrock-gateway-key`) and other secrets. | Reference Terraform | [cloud_adapters/aws/secrets.py](../../cloud_adapters/aws/secrets.py) — `SecretsManagerProvider` |
| 6 | DynamoDB (`galaxy-trace-ledger`) | Persistent hash-chained `trace_ledger` archive — partition key `run_id`, sort key `entry_seq`. Survives restarts; queryable for compliance. When unreachable the chain is still built and verified in memory. | Reference Terraform | [cloud_adapters/aws/audit.py](../../cloud_adapters/aws/audit.py) — `DynamoDbHashChainBackend` |
| 7 | ADOT collector → X-Ray / CloudWatch | OTel span sink. The app exports OTLP to an ADOT collector, which re-exports to X-Ray (traces) and CloudWatch (metrics/logs). | Reference Terraform | [cloud_adapters/aws/tracing.py](../../cloud_adapters/aws/tracing.py) — `AwsTraceExporterFactory` |

Live IDs and endpoints are scrubbed and kept out of the repository. Populate them into a local `.env` from [.env.example](../../.env.example); the values are obtained from `terraform output`.

### 1.2 Method 2 — Amazon Bedrock AgentCore (live, us-east-2)

> Deployed on account `<ACCOUNT_ID>`, region `us-east-2`, by [`scripts/deploy_agentcore.py`](../../scripts/deploy_agentcore.py). The script is idempotent; `--teardown` reverses the provisioning. The enforcement split here is: Cedar performs coarse authorization (which tool, which recipient); the interceptor Lambdas perform content controls (prompt injection, credential handling, PII); the data proxy performs fine-grained access control (FGAC).

| # | Resource | What it does | Status | Where it touches code |
|---|---|---|---|---|
| 1 | MCP Gateway (`galaxy-governance-gw`) | The AgentCore Gateway the runtimes call (protocolType `MCP`, `AWS_IAM` auth). Tool calls flow through it and through the bound interceptors. | Live (us-east-2) | [scripts/deploy_agentcore.py](../../scripts/deploy_agentcore.py) |
| 2 | Cedar policy engine (`galaxy_governance`, ENFORCE) | Coarse tool authorization in ENFORCE mode. One permit/forbid policy per (agent × tool) — 3 agents × 3 tools = 9 policies over tools `query_billing`, `summarize_costs`, `query_dataset`. Action form `galaxy-tools___query_billing`; principal `arn:aws:sts::<ACCOUNT_ID>:assumed-role/galaxy-rp-<type>`. | Live (us-east-2) | Generated by [galaxy_gov/agentcore/cedar_export.py](../../galaxy_gov/agentcore/cedar_export.py) |
| 3 | Interceptor Lambda — request (`galaxy-gov-request`) | Bound at the gateway REQUEST interception point. Handler `request_interceptor.handler` — prompt-injection (medium), credential deny, blocked patterns. Zip package (ECR is SCP-blocked, so zip rather than container). | Live (us-east-2) | [scripts/build_interceptor_zip.sh](../../scripts/build_interceptor_zip.sh) → `.build/interceptor.zip` |
| 4 | Interceptor Lambda — response (`galaxy-gov-response`) | Bound at the gateway RESPONSE interception point. Handler `response_interceptor.handler` — output PII redaction. Same zip package. Env `GOV_POLICY_REGISTRY_PATH=/var/task/agent-controls.json`. | Live (us-east-2) | [scripts/build_interceptor_zip.sh](../../scripts/build_interceptor_zip.sh) |
| 5 | AgentCore Runtimes (`galaxy_finops`, `galaxy_auditor`, `galaxy_rogue`) | One per persona (`PYTHON_3_12`, entrypoint `runtime_agent.py`). Env `AGENT_TYPE`, `GW_URL`, `GW_TARGET=galaxy-tools`. Each runs under role `galaxy-rp-<type>`. Code artifact in S3 bucket `galaxy-agentcore-runtime-<ACCOUNT_ID>`; optional `.build/runtime.zip` vendors `aws-opentelemetry-distro` for GenAI observability. | Live (us-east-2) | [scripts/deploy_agentcore.py](../../scripts/deploy_agentcore.py) |
| 6 | Stub tool Lambda (`galaxy-tools`) | Backs the `query_billing` / `summarize_costs` / `query_dataset` tools behind the gateway target. Runs under role `galaxy-tool-lambda`. | Live (us-east-2) | [scripts/deploy_agentcore.py](../../scripts/deploy_agentcore.py) |
| 7 | Gateway execution role (`galaxy-agentcore-gateway`) | The execution role the gateway assumes to invoke targets and interceptors. | Live (us-east-2) | [scripts/deploy_agentcore.py](../../scripts/deploy_agentcore.py) |

The per-persona NHI roles are `galaxy-rp-finops` / `galaxy-rp-auditor` / `galaxy-rp-rogue`. These roles serve as both the Cedar principals and the runtime execution roles.

---

## 2. External services

| Service | Used today? | Notes |
|---|---|---|
| **Amazon Bedrock** (Converse) | Every live LLM call | The agents reach Bedrock through the API Gateway chokepoint (`BedrockGatewayChatModel`), not `bedrock-runtime` directly. Model selected via `AWS_BEDROCK_MODEL_ID`. **Not used by the offline demo.** |
| **API Gateway → Lambda → Bedrock** | Every live LLM call | The governed egress path. The Lambda proxy SigV4-signs to Bedrock; the agent only carries the gateway `x-api-key`. |
| **AWS Secrets Manager / SSM** | Live runs | Stores the gateway key (`galaxy/bedrock-gateway-key`) and other secrets, read by `SecretsManagerProvider`. |
| **AWS X-Ray / CloudWatch** (via ADOT) | Live runs | OTel spans flow OTLP → ADOT collector → X-Ray / CloudWatch when an OTLP endpoint is configured. |
| **Amazon Bedrock AgentCore** (Gateway, Runtime, Policy engine) | Method 2 live runs (us-east-2) | The MCP Gateway `galaxy-governance-gw`, the `galaxy_governance` Cedar engine (ENFORCE), the `galaxy-gov-request` / `galaxy-gov-response` interceptors, and the per-persona runtimes. Provisioned by `scripts/deploy_agentcore.py`. |
| **Docker Hub** (`registry-1.docker.io`) | Blocked by corporate proxy | Anonymous CDN paths return 403; never pulled directly. ECR is SCP-blocked, so the AgentCore interceptors ship as zip packages rather than container images. |

---

## 3. Python runtime stack ([requirements.txt](../../requirements.txt))

### 3.1 Governance packages — `agent_os` / `agent_sre` / `agentmesh` (policies + audit + circuit breaker)

| Package | Version | Role |
|---|---|---|
| `agent-os-kernel` | `>=3.7.0` (verified **3.7.0**) | The runtime governance engine. Provides `agent_os.policies.PolicyEvaluator`, `agent_os.audit_logger.GovernanceAuditLogger`, `agent_os.circuit_breaker.CircuitBreaker`, `agent_os.prompt_injection.PromptInjectionDetector`, and `agent_os.integrations.maf_adapter` (the MAF middleware this repo wraps). |
| `agent-sre` | `>=3.7.0` (verified **3.7.0**) | Provides `agent_sre.anomaly.RogueAgentDetector`, imported by `agent_os.integrations.maf_adapter`. **WS3:** the former `==3.2.2` exact pin was released — kernel was already 3.7.0, and 3.7.0 keeps the same symbol; the maf_adapter import + full suite verified green, so all three governance packages now align at 3.7.0. |
| `agentmesh-platform` | `>=3.7.0` (verified **3.7.0**) | Required transitively by `agent_os.integrations.maf_adapter` (`from agentmesh.governance import AuditEntry, AuditLog`). Without it, imports fail. |

### 3.2 AWS SDK — identity, secrets, egress, ledger, tracing (`.[aws]` extra)

This stack is installed via `pip install '.[aws]'`. The agent does not hold Bedrock credentials; it reaches Bedrock only through the API Gateway chokepoint.

| Package | Version | Role |
|---|---|---|
| `boto3` | `>=1.34.0` | AWS SDK for Python. Provides STS (`assume_role` for per-agent identity), Secrets Manager / SSM (gateway key + secrets), DynamoDB (`galaxy-trace-ledger` audit backend), and Bedrock client access used by the Lambda proxy. |
| `opentelemetry-exporter-otlp-proto-grpc` | `>=1.27.0` | OTLP span exporter pointed at the ADOT collector, which re-exports to X-Ray / CloudWatch. |

### 3.3 LangChain frameworks (the orchestration axis)

These frameworks are installed via the framework extras in `pyproject.toml`. Each adapter calls the same shared `GuardPipeline`.

| Package | Version | Role |
|---|---|---|
| `langchain` / `langgraph` / `langchain-openai` | `>=1.0` | The LangGraph adapter (`--framework langgraph`, default), via the `.[langgraph]` extra. The Pydantic AI and raw (provider-native) adapters have their own extras. |

### 3.4 OpenTelemetry

| Package | Version | Role |
|---|---|---|
| `opentelemetry-api` | `>=1.27.0` | `trace.get_tracer`, span context, propagation. |
| `opentelemetry-sdk` | `>=1.27.0` | `TracerProvider`, `BatchSpanProcessor`, `Resource`. |
| `opentelemetry-exporter-otlp-proto-grpc` | `>=1.27.0` | OTLP exporter to the ADOT collector (X-Ray / CloudWatch). |
| `opentelemetry-instrumentation-fastapi` | `>=0.48b0` | Auto-instrumentation for the future human-gate FastAPI endpoint. |

### 3.5 Storage / config / web

| Package | Version | Role |
|---|---|---|
| `pydantic` | `>=2.0.0,<3` | Schema validation for `payload_agents/config/*.yaml`. Pinned explicitly so dependency bumps can't drag us across major Pydantic lines. |
| `PyYAML` | `>=6.0.1` | Reads YAML config + governance policies. |
| `python-dotenv` | `>=1.0.0` | Loads `.env` for local dev. |
| `fastapi` + `uvicorn` | `>=0.115` / `>=0.32` | Future human-gate endpoint. Not currently mounted. |

### 3.6 Test

| Package | Version | Role |
|---|---|---|
| `pytest` | `>=8.0.0` | Test runner. |
| `pytest-asyncio` | `>=0.24.0` | `@pytest.mark.asyncio` for the async governance tests. |

---

## 4. Local tooling

| Tool | Why it's used | Notes |
|---|---|---|
| `python` 3.13 / 3.14 | Runtime | Both work. |
| `uv` | pip resolver / venv manager | Used for `uv venv` / `uv pip install` / `uv run`. |
| `aws` (AWS CLI) | AWS auth + queries for live cloud runs | SSO/profile login (`AWS_PROFILE`). Not needed for the offline demo or tests. |
| `terraform` | Provisions the AWS infra | Runs against [`cloud_adapters/aws/infra/`](../../cloud_adapters/aws/infra/); `terraform apply` / `terraform output` / `terraform destroy`. Resources tagged `galaxy-rp`. |
| `git` | Source control | — |
| `gh` (GitHub CLI) | GitHub operations | Used as needed. |

---

## 5. Governance policies (YAML on disk)

The `*.yaml` policy packs are loaded by `agent_os.policies.PolicyEvaluator` at agent build time. All files in `galaxy_gov/policies/` are loaded automatically, and no manifest is required. The LangGraph guard ([framework_adapters/langgraph/guard.py](../../framework_adapters/langgraph/guard.py)) wires the evaluator into the shared `GuardPipeline`.

| File | What it enforces |
|---|---|
| [galaxy_gov/policies/galaxy-core.yaml](../../galaxy_gov/policies/galaxy-core.yaml) | Prompt-injection regex (OWASP ASI-01) + oversized-prompt gate |
| [galaxy_gov/policies/galaxy-tools.yaml](../../galaxy_gov/policies/galaxy-tools.yaml) | Per-agent tool allow-list. FinOps and Auditor declare their read tools; Rogue ships with `allowed_tools: []`, so every tool it attempts is denied. |
| [galaxy_gov/policies/galaxy-pii.yaml](../../galaxy_gov/policies/galaxy-pii.yaml) | PII rules placeholder — `defaults.action=allow` (no-op) until a PII detector is wired |
| [galaxy_gov/policies/galaxy-ast.yaml](../../galaxy_gov/policies/galaxy-ast.yaml) | **(archived)** AST-agent-specific rules (deny outbound A2A from leaf agent, etc.) |

Two further guard configurations are read by the pre-middleware guards rather than by `PolicyEvaluator`:

| File | What it tunes |
|---|---|
| [cloud_adapters/aws/egress.yaml](../../cloud_adapters/aws/egress.yaml) | Outbound network egress allow-list — the API-Gateway / Bedrock hosts as the only permitted LLM destinations |
| [galaxy_gov/configs/prompt-injection.yaml](../../galaxy_gov/configs/prompt-injection.yaml) | Injection threat patterns + scoring thresholds |

### Per-agent config (separate from policies)

Three per-agent configurations are provided, one per persona:

| File | What it tunes |
|---|---|
| [payload_agents/config/finops.yaml](../../payload_agents/config/finops.yaml) | `context_budget_tokens=40000`, `prompt_injection_block_threshold=high`, `credential_mode=redact`, `allowed_recipients=[Auditor]`, FGAC/drift/reasoning toggles on, `allowed_tools=[query_billing, summarize_costs]` |
| [payload_agents/config/auditor.yaml](../../payload_agents/config/auditor.yaml) | The leaf-callee persona — runs A2A requests inside its own guard pipeline |
| [payload_agents/config/rogue.yaml](../../payload_agents/config/rogue.yaml) | The deny-all persona — a valid NHI but no policy, so every action is denied (`context_budget_tokens=200`, `credential_mode=deny`, `allowed_tools=[]`) |

---

## 6. Environment variables (the `.env` contract)

The offline demo (`scripts/demo_governance.py`) and the test suite require **none** of these variables. The variables below apply only to a live `build_*_agent()` run against Bedrock. The endpoint and key are obtained from `terraform output`. For the full template, see [.env.example](../../.env.example).

| Variable | Purpose | Required? | Read at |
|---|---|---|---|
| `AWS_BEDROCK_GATEWAY_ENDPOINT` | The API Gateway `/invoke` URL — when set, all agents route through the gateway chokepoint | Required for live `--aws` | [cloud_adapters/aws/gateway.py:37](../../cloud_adapters/aws/gateway.py#L37) |
| `AWS_BEDROCK_GATEWAY_KEY` | Local fallback for the gateway `x-api-key` (Secrets Manager preferred when deployed) | Optional (Secrets Manager preferred) | [cloud_adapters/aws/gateway.py:43](../../cloud_adapters/aws/gateway.py#L43) via `SecretsManagerProvider(secret_name="galaxy/bedrock-gateway-key")` |
| `AWS_BEDROCK_MODEL_ID` | Bedrock model id (e.g. `us.anthropic.claude-sonnet-4-6`) | Required for live `--aws` | [framework_adapters/langgraph/bedrock_gateway.py](../../framework_adapters/langgraph/bedrock_gateway.py) |
| `AWS_PROFILE` | SSO/profile used to resolve AWS credentials (or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) | Required for live `--aws` | boto3 default credential chain |
| `AWS_REGION` | Region for STS / Secrets Manager / DynamoDB / Bedrock | Optional (defaults to `us-east-1`) | [cloud_adapters/aws/secrets.py:49](../../cloud_adapters/aws/secrets.py#L49) and peers |
| `GALAXY_LEDGER_TABLE` | DynamoDB ledger table name | Optional (defaults to `galaxy-trace-ledger`) | [cloud_adapters/aws/audit.py:50](../../cloud_adapters/aws/audit.py#L50) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The ADOT collector endpoint (OTLP → X-Ray / CloudWatch); tracing is a no-op when unset | Optional (recommended for live runs) | [cloud_adapters/aws/tracing.py:31](../../cloud_adapters/aws/tracing.py#L31), [core/run_tracer.py](../../core/run_tracer.py) |
| `OTEL_SERVICE_NAME` | OTel resource attribute | Optional (code default `galaxy-platform`) | [core/run_tracer.py:54](../../core/run_tracer.py#L54) |
| `NHI_CLIENT_ID_FINOPS` / `_AUDITOR` / `_ROGUE` | The three personas' NHI principal ids; flow into `agent_id` and every audit row's `nhi_id` | Required for live runs (placeholder OK locally) | [cloud_adapters/aws/identity.py:40](../../cloud_adapters/aws/identity.py#L40), [core/nhi_registry.py](../../core/nhi_registry.py) |

Setting `CLOUD_PROVIDER=aws` selects the AWS adapter, comprising the DynamoDB ledger, Secrets Manager, and the API Gateway egress path. When `CLOUD_PROVIDER` is not set, the offline path is used.

### 6.1 AgentCore (Method 2) variables

These variables are set on the AgentCore resources by [`scripts/deploy_agentcore.py`](../../scripts/deploy_agentcore.py) and are not part of the local `.env` contract described above. They are listed here for reference.

| Variable | Purpose | Set on |
|---|---|---|
| `AGENT_TYPE` | Selects the persona (`finops` / `auditor` / `rogue`) the runtime loads | Each AgentCore Runtime (`galaxy_finops` / `galaxy_auditor` / `galaxy_rogue`) |
| `GW_URL` | The `galaxy-governance-gw` MCP Gateway URL the runtime calls | Each AgentCore Runtime |
| `GW_TARGET` | The gateway target name (`galaxy-tools`) | Each AgentCore Runtime |
| `GOV_POLICY_REGISTRY_PATH` | Path to the bundled policy registry (`/var/task/agent-controls.json`) the interceptors read | The interceptor Lambdas (`galaxy-gov-request` / `galaxy-gov-response`) |

---

## 7. Telemetry attribute vocabulary

This section describes the OTel span and event attributes that flow through the ADOT collector to X-Ray and CloudWatch. The attributes are drawn from the following sources:

- **GenAI semantic conventions**: `gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.agent.name`, `gen_ai.tool.name`.
- **Galaxy pipeline attributes** (on the `pipeline.run` root span emitted by `pipeline_span()` in [core/run_tracer.py](../../core/run_tracer.py)): `galaxy.run_id`, `galaxy.module`. These are the **only** `galaxy.*` keys on span dimensions; `galaxy.nhi_id`, `galaxy.agent_type`, and `galaxy.attempt` are **not** span attributes.
- **A2A attributes** ([core/a2a/dispatcher.py](../../core/a2a/dispatcher.py)): `a2a.conversation_id`, `a2a.message_id`, `a2a.sender`, `a2a.recipient`, `a2a.intent`, `a2a.payload_schema`, `a2a.status`, `a2a.latency_ms`, `a2a.request_envelope`, `a2a.response_envelope` (truncated to 8 KB each).
- **Governance audit attributes** (emitted by `OtelAuditBackend` as *span events*, not span attributes — [galaxy_gov/adapters/otel_audit_backend.py](../../galaxy_gov/adapters/otel_audit_backend.py)): `governance.agent_id` (NHI principal, e.g. `FinOps-<client-id>`), `governance.event_type`, `governance.action`, `governance.decision`, `governance.reason`, `governance.latency_ms`, plus arbitrary scalar metadata as `governance.metadata.<key>`.

**NHI attribution** is available only via `governance.agent_id` in governance audit events. It is not present on the OTel span dimensions directly.

---

## 8. Where each piece is configured (one-liner index)

| Concern | Configured in | Read by |
|---|---|---|
| Per-agent runtime tunables | [payload_agents/config/*.yaml](../../payload_agents/config/) | [galaxy_gov/agent_config.py](../../galaxy_gov/agent_config.py) |
| Runtime governance rules | [galaxy_gov/policies/*.yaml](../../galaxy_gov/policies/) | `agent_os.policies.PolicyEvaluator` via the LangGraph guard ([framework_adapters/langgraph/guard.py](../../framework_adapters/langgraph/guard.py)) |
| Pre-middleware guard configs | [galaxy_gov/configs/*.yaml](../../galaxy_gov/configs/) | the prompt-injection / egress guards |
| NHI registry | [core/nhi_registry.py](../../core/nhi_registry.py) | `NHIRegistry.get(agent_type)` |
| LLM model + gateway key + egress | `.env` (local) / Secrets Manager (deployed) | [cloud_adapters/aws/gateway.py](../../cloud_adapters/aws/gateway.py), [cloud_adapters/aws/secrets.py](../../cloud_adapters/aws/secrets.py) |
| OTel exporter routing | `.env` `OTEL_EXPORTER_OTLP_ENDPOINT` | [cloud_adapters/aws/tracing.py](../../cloud_adapters/aws/tracing.py), [core/run_tracer.py](../../core/run_tracer.py) |
| DynamoDB ledger backend | `GALAXY_LEDGER_TABLE` (default `galaxy-trace-ledger`) | [cloud_adapters/aws/audit.py](../../cloud_adapters/aws/audit.py) |
| AWS infra — Method 1 (gateway, IAM, ledger, ADOT) | [cloud_adapters/aws/infra/](../../cloud_adapters/aws/infra/) | `terraform apply` |
| AWS infra — Method 2 (AgentCore gateway, Cedar, interceptors, runtimes) | [scripts/deploy_agentcore.py](../../scripts/deploy_agentcore.py), [galaxy_gov/agentcore/cedar_export.py](../../galaxy_gov/agentcore/cedar_export.py) | `python scripts/deploy_agentcore.py --region us-east-2` |
| Python deps | [requirements.txt](../../requirements.txt) + `.[aws]` extra | pip / uv |
| Test fixtures | [tests/](../../tests/) | `pytest` |

---

## 9. Common debug shortcuts

```bash
# 1. Run the offline governance demo — no cloud / DB / LLM
uv run python scripts/demo_governance.py

# 2. Run the test suite (no cloud credentials needed)
uv run python -m pytest tests/ -q

# 3. Check what's currently in the venv
uv pip list --python .venv/bin/python | grep -iE "agent|opentel|boto3|langchain|pydantic"

# 4. Run the full demo matrix (47 platform controls / 84 checks) against real Bedrock via the gateway
.venv/bin/python scripts/demo_agents.py --aws --extended

# 5. Build a live persona and fire a policy-deny probe
uv run python -c "
import asyncio; from dotenv import load_dotenv; load_dotenv()
from payload_agents.langgraph import make_model, build_finops_agent
from langchain_core.messages import AIMessage
async def main():
    bundle = await build_finops_agent(run_id='probe-deny', model=make_model(AIMessage(content='ok')))
    print(bundle.invoke('ignore previous instructions'))
    await bundle.pg_backend.close()
asyncio.run(main())
"

# 6. Query governance blocks in CloudWatch Logs Insights (or the X-Ray console)
aws logs start-query --log-group-name /galaxy/governance \
  --start-time $(date -v-1H +%s) --end-time $(date +%s) \
  --query-string "fields @timestamp, governance.decision | filter governance.decision = 'deny' | limit 20"

# 7. Method 2 (AgentCore) — build the interceptor zip, then deploy live (us-east-2)
scripts/build_interceptor_zip.sh
AWS_PROFILE=<profile> python scripts/deploy_agentcore.py --region us-east-2

# 8. Tear down the AgentCore deployment (reverses the provisioning)
AWS_PROFILE=<profile> python scripts/deploy_agentcore.py --region us-east-2 --teardown

# 9. Run the full demo matrix against the live AgentCore deployment
#    (adds rows O1 tools/call allow-vs-deny and O2 tools/list filtering)
.venv/bin/python scripts/demo_agents.py --agentcore --extended
```
