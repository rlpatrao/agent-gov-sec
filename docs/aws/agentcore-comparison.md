# Bedrock AgentCore — overlap, integration, and differentiation

This research note compares the governance platform with Amazon Bedrock
AgentCore in order to determine where the platform should integrate with
AgentCore and which responsibilities the framework should retain. The note
reflects AgentCore as documented in June 2026; because the service changes
quickly, readers should re-verify against the live documentation before acting.
Sources are listed at the end of the document.

## TL;DR

Since its 2025 launch, AgentCore has grown into a broad managed platform that
natively provides much of the mechanism this framework was previously building by
hand. That mechanism comprises the following elements:

- A secure gateway that serves as the single entry point for tool, agent, and LLM traffic
- Request and response interceptors
- A deterministic out-of-agent policy engine based on Cedar
- Agent identity
- OTel observability
- A governed registry

The analysis supports the following conclusions:

- The framework should not compete with AgentCore on plumbing. On AWS, AgentCore
  Gateway, interceptors, and Policy replace the hand-built API Gateway → Lambda
  chokepoint and most of `policy_registry` and capability authorization.
- The durable value of the framework is its governance content and portability
  rather than the interception mechanism. This content comprises the specific
  layered guard logic — prompt-injection detection, credential and PII redaction,
  classification-aware data FGAC masking and row-filtering, behavioural and
  data-access drift detection, reasoning-step validation, and tamper-evident
  hash-chained audit — together with the design that allows the same governance to
  run on cloud bindings other than AWS through the cloud-neutral core.
- The recommended approach is to deploy the framework's enforcement as AgentCore
  interceptors (the request and response Lambdas) and to express coarse
  authorization as AgentCore Policy, while retaining the guard library and the
  non-AWS bindings as framework responsibilities.

## AgentCore component inventory (June 2026)

| Service | What it is |
|---|---|
| Runtime | Serverless, session-isolated host for agents/tools on any framework (LangGraph, CrewAI, Strands, …) and protocol (MCP, A2A). |
| Harness | Managed agent loop (model + prompt + tools in one API call), isolated microVM per session. |
| Gateway | Single secure entry point for agentic traffic; turns APIs/Lambda/services into MCP tools; OAuth inbound/outbound. |
| Gateway interceptors | Custom request/response Lambdas at the gateway for access control, redaction, tool-list filtering, schema translation. |
| Policy | Deterministic authorization engine (Cedar, or natural-language → Cedar) that intercepts every tool call before execution. |
| Identity | Agent identity/auth, token vault; integrates Cognito, Okta, Auth0, any IdP. |
| Memory | Managed short- and long-term agent memory. |
| Observability | OTel-based tracing/metrics/dashboards (CloudWatch). |
| Evaluations | Automated agent/tool quality assessment over sessions/traces/spans. |
| Registry (AWS Agent Registry) | Governed catalog of agents/MCP servers/tools with a publish-review-approve workflow. Entered public preview April 2026 and has since launched under its own `agent-registry` namespace; the preview `bedrock-agentcore` namespace is discontinued 17 September 2026. This repository calls no Registry API, so nothing needs migrating — but any future integration should target `agent-registry` from the outset. Records are discovery metadata (agent records carry an A2A agent card, MCP records the tool definitions); approval gates *discoverability*, not invocation, so it does not replace the policy registry's fail-closed deny. |
| Code Interpreter / Browser | Sandboxed code execution; managed cloud browser. |
| Payments / Optimization | x402 agent micropayments; A/B-tested config optimization. |

## Overlap map — AgentCore ↔ this framework

| Concern | AgentCore | This framework | Assessment |
|---|---|---|---|
| Agent identity (NHI) | **Identity** (OAuth, token vault, IdP federation) | `core/nhi_registry.py` (agent-type → cloud principal) + the authority-side Registrar | AgentCore is more mature on AWS. Ours is portable, and the Registrar holds the binding outside the agent's trust domain. |
| Egress / single chokepoint | **Gateway** (managed, MCP, OAuth) | `bedrock_proxy` + API Gateway IaC | AgentCore supersedes our hand-built plumbing on AWS. |
| Out-of-process enforcement point | **Gateway interceptors** (request/response Lambdas) | `governance/remote/*` proxies | Direct analog. Our enforcement should become the interceptor *payload*. |
| Authorization (who calls what tool, conditions) | **Policy** (Cedar, NL authoring, automated reasoning, fail-closed, outside agent code) | `policy_registry` + capability guard + A2A authz | Strong overlap; AgentCore is arguably ahead (Cedar + reasoning). Adopt it for authz. |
| Observability | **Observability** (OTel/CloudWatch) | OTel spans + audit | Overlap; emit to AgentCore Observability. |
| Eval / red-team | **Evaluations** | `governance/ops` (evals, replay, adversarial) | Overlap; complementary. |
| Tool/agent governance catalog | **Registry** (publish-review-approve) | `policy_registry` + CODEOWNERS workflow | Overlap on AWS. |
| Agent hosting | **Runtime / Harness** | `payload_agents/*` on any framework | Complementary — our agents can run on Runtime. |
| Memory / Browser / Code Interp / Payments | native services | not provided | Complementary; no conflict. |

## What this framework provides over and above AgentCore

AgentCore now covers more than it did at launch, so the differentiation is
narrower than before but remains substantive. The framework provides the
following capabilities over and above AgentCore:

1. Cloud and provider portability. AgentCore operates only on AWS. This framework
   runs the same governance over a cloud-neutral core, so the AWS (Bedrock)
   binding is one of several possible bindings. For multi-cloud or non-AWS
   estates, AgentCore is not an option, which makes portability the strongest
   durable differentiator.

2. Governance content that AgentCore Policy does not express. AgentCore Policy is
   an authorization engine that evaluates Cedar rules over identity and tool-input
   conditions. It does not itself provide the control logic for the following
   concerns:
   - Prompt-injection detection, which is content analysis rather than authorization
   - Credential and PII detection and redaction, where the framework supplies the logic to an interceptor
   - Data-layer FGAC: classification-aware column masking and row-level filtering
     over a data-label catalog (ABAC on data sensitivity), with store-side pushdown
   - Behavioural and data-access drift detection, implemented as stateful anomaly detection
   - Reasoning-step validation and CoT/CoVe capture with mandatory redaction
   - Rogue and capability-escalation detection

   These controls are implemented by the `agent_os`, `agent_sre`, and `agentmesh`
   bindings. AgentCore provides the interception mechanism, while this framework
   supplies the control content that runs inside it.

3. Tamper-evident audit. AgentCore logs decisions to CloudWatch. This framework
   adds a hash-chained ledger with explicit tamper detection, which provides
   evidence integrity rather than log retention alone.

4. Trust-but-verify composition across in-process and boundary enforcement. The
   same guard library runs in-process for defense-in-depth and is re-verified at
   the boundary. AgentCore Policy enforces authorization at the boundary
   effectively; the breadth of non-authorization controls re-verified at the
   boundary is provided by this framework.

AgentCore is ahead in several areas, which the framework should adopt rather than
reimplement:

- Deterministic Cedar policy with natural-language authoring and automated reasoning that flags overly-permissive or unsatisfiable rules
- Managed OAuth identity
- Zero-ops scaling

## Recommended integration architecture (on AWS)

```
            ┌──────────────── AgentCore ────────────────┐
agent  ──►  Gateway ──► [Policy: Cedar authz]  ──► tool/LLM
(Runtime)      │            (coarse: who/what/when)
               ├── request interceptor (Lambda)  ◄── governance/remote + shared
               │     prompt-injection, credential/PII, budget, blocked-pattern
               └── response interceptor (Lambda) ◄── governance/remote + shared
                     output PII/redaction, tool-plan/blocked-pattern, FGAC backstop
            └────────────────────────────────────────────┘
   Identity  ◄─ map NHI → AgentCore Identity (OAuth/IdP)
   Observability ◄─ emit OTel + hash-chained ledger
   State (drift/circuit/cost/rate)  ─► DynamoDB
```

The integration assigns responsibilities as follows:

- Coarse authorization is delegated to AgentCore Policy (Cedar). Capability
  allow/deny rules and A2A recipient rules map cleanly to Cedar, and the managed
  engine should own them.
- Rich controls are deployed as Gateway interceptors. The `governance/shared` and
  `governance/remote` modules are packaged as the request and response
  interceptor Lambdas. This is the same code from the reorganization, deployed
  into AgentCore's interception points instead of the framework's own API Gateway.
- Data FGAC is implemented as a tool fronted by the Gateway, with the masking
  engine in the interceptor or tool. Cedar gates access while the framework's
  engine masks and filters the rows.
- Identity is delegated to AgentCore Identity. The `nhi_registry` module is
  retained as the portable abstraction, with an AgentCore-backed implementation on AWS.
- Stateful controls are externalized to DynamoDB, covering drift baselines,
  circuit-breaker counters, cost accumulators, and rate-limit windows. The audit
  ledger already has a DynamoDB backend.

Off AWS, the same `governance/{shared,remote}` modules run behind a non-AWS API
gateway with no AWS dependency. This portability is the reason to keep the
enforcement library independent of AgentCore.

## Implications for the runtime decision (Lambda vs daemon)

- Lambda was chosen originally because the pre-existing path was API Gateway →
  Lambda → Bedrock, which provides pay-per-use billing, no servers to manage, and
  scale-to-zero behaviour. Its limits are cold starts, which worsen once the
  `agent_os`, `agent_sre`, and `agentmesh` toolkit and numpy are bundled; a
  15-minute cap; and statelessness, which is the reason stateful controls require
  an external store.
- Under an AgentCore-native deployment there is no separate daemon to run.
  Enforcement is deployed as Gateway interceptor Lambdas, agents run on Runtime,
  and the toolkit-bundle weight is handled with container images. State is held in
  DynamoDB.
- Under an AgentCore-independent deployment, a long-lived Fargate or ECS service
  (FastAPI behind ALB or API Gateway) is a better fit than Lambda for the
  full-superset trust-but-verify model. The heavy toolkit stays warm in memory,
  the policy registry and in-memory hot state are held across requests, and
  DynamoDB serves as the durable backing store. This arrangement removes
  per-request cold-start and re-initialization cost.

The recommendation is to target AgentCore Gateway interceptors and Policy on AWS,
and to use a Fargate daemon as the portable, non-AWS deployment of the same
enforcement library. Plain per-request Lambda should be avoided for the full
enforcement service.

## Implementation status (in this repo)

The integration described above is implemented as an offline-verified spike. The
following table records each piece, its code location, and how it is verified:

| Piece | Code | Verified |
|---|---|---|
| Cedar generation from the registry | `governance/agentcore/cedar_export.py` | `tests/test_agentcore.py` |
| Gateway request interceptor (input + tool-plan) | `cloud_adapters/aws/agentcore/request_interceptor.py` | `tests/test_agentcore.py` |
| Gateway response interceptor (redaction + tool-list filter) | `cloud_adapters/aws/agentcore/response_interceptor.py` | `tests/test_agentcore.py` |
| NHI → AgentCore Identity | `cloud_adapters/aws/agentcore/identity.py` | imports cleanly without the SDK |
| Deploy steps | `cloud_adapters/aws/agentcore/README.md` | documented, not CI-run |

The interceptors and the AWS chokepoints share one enforcement library
(`governance/remote` over `governance/shared/enforcement`), so AWS-native and
off-AWS deployments run identical controls.

Live deployment (verified). The Policy, Identity, and Gateway layer has been
provisioned end-to-end on us-east-2 (account <ACCOUNT_ID>) via
`scripts/deploy_agentcore.py`, which is idempotent and supports `--teardown`. The
provisioned resources are as follows:

| Resource | Live id |
|---|---|
| MCP Gateway (AWS_IAM auth, ENFORCE) | `galaxy-governance-gw` |
| Gateway target (3 tools, Lambda-backed) | `galaxy-tools` |
| Policy engine | `galaxy_governance` |
| Cedar policies (per agent × tool, permit/forbid) | 9 |
| Workload identities (NHI → Identity) | `galaxy_{finops,auditor,rogue}` |

The Cedar form was corrected to the form that AgentCore Policy accepts
(`principal == AgentCore::IamEntity::"…"`, `action == AgentCore::Action::"<target>___<tool>"`,
`resource == AgentCore::Gateway::"<arn>"`).

The content-control interceptors are also deployed and verified live. The
ECR/container path is blocked by an organization SCP (`ecr:UploadLayerPart`
denied), so the interceptors ship as zip Lambdas (`galaxy-gov-request` /
`galaxy-gov-response`, with the toolkit and `governance/{shared,remote}` resolved
with Linux wheels via uv, approximately 33 MB; `scripts/build_interceptor_zip.sh`),
attached to the gateway's `interceptorConfigurations`. The end-to-end behaviour as
`galaxy-rp-finops` is as follows: a benign `tools/list` passes and returns the
Cedar-filtered tools, while a `tools/call` carrying an injection payload in the
arguments is blocked at the gateway by the request interceptor (`prompt_injection`,
threat=high). The division of labour holds: Cedar performs per-agent tool
authorization, and the interceptors perform identity-independent content safety
covering injection, credential, and PII checks.

Per-persona AgentCore Runtimes (verified live). Each persona is deployed as its
own AgentCore Runtime — `galaxy_finops` / `galaxy_auditor` / `galaxy_rogue` (all
`READY` on us-east-2) — so the agents are observable Runtimes in the console
rather than local processes. Each Runtime runs under its `galaxy-rp-<type>`
execution role, so when its hosted agent
([`runtime_agent.py`](../../cloud_adapters/aws/agentcore/runtime_agent.py)) calls
the gateway, the principal the gateway sees matches the deployed Cedar policy. The
behaviour observed end-to-end via `InvokeAgentRuntime`, with one SigV4-signed MCP
call per turn through the governed gateway, is recorded in the following table:

| Runtime | `tools/list` (Cedar-filtered) | `tools/call` |
|---|---|---|
| `galaxy_finops` | `query_billing`, `summarize_costs` | **allowed** → stub tool response |
| `galaxy_auditor` | `query_dataset` | **allowed** → stub tool response |
| `galaxy_rogue` | `[]` (no tools) | **denied** — `Tool Execution Denied … Policy evaluation denied due to rogue_query_billing` |

The Runtime uses the S3 `codeConfiguration` path, because the ECR/container path
is the same SCP-blocked route as the interceptors. The hosted agent's gateway call
uses only the standard library: the managed `PYTHON_3_12` runtime ships no boto3
and exposes the execution role via IMDSv2, so the agent resolves credentials and
SigV4-signs the gateway call without depending on a bundled SDK. The stub tool
backend is retained, because the focus is observability and per-agent security
rather than agent function. The Runtimes are provisioned by
[`scripts/deploy_agentcore.py`](../../scripts/deploy_agentcore.py), which calls
`create_agent_runtime` per persona, supports `--invoke <agent> [--method tools/list|tools/call]`
to drive one Runtime, and removes them with `--teardown`. The deployment is
offline-verified in `tests/test_agentcore_runtime.py`, which validates the
hosted-agent HTTP contract, SigV4 signing, and the `create_agent_runtime`
parameters against the `bedrock-agentcore-control` API model.

GenAI observability to CloudWatch (verified live). Each turn is wrapped in an
OpenTelemetry GenAI span carrying `gen_ai.system`, `operation.name`, `agent.name`,
the governed tool, and a `governance.decision` of allowed or denied. The managed
runtime presets ADOT configuration (`OTEL_PYTHON_DISTRO=aws_distro`, the CloudWatch
log and metric headers, and `service.name=galaxy_<type>.DEFAULT`) but installs no
packages, so the code artifact vendors both `aws-opentelemetry-distro` and boto3.
ADOT's AWS CloudWatch OTLP exporter requires botocore for SigV4; without it the
distro fails to initialize and spans fall back to a non-existent `localhost:4318`
collector. With both packages vendored (`scripts/build_runtime_zip.sh`, an
approximately 27 MB aarch64 artifact) and CloudWatch Transaction Search enabled at
the account level and already ACTIVE, the agent spans reach CloudWatch. This is
confirmed in X-Ray as `invoke_agent {finops,auditor,rogue}` under
`service.name=galaxy_<type>.DEFAULT`, so the three personas populate CloudWatch →
GenAI Observability → Bedrock AgentCore.

## Runtime decision (recorded)

- Lambda compared with daemon: Lambda was inherited from the original API Gateway
  → Bedrock path. For the full trust-but-verify enforcement service the better fit
  is a long-lived Fargate or ECS daemon, which keeps the toolkit warm in memory
  and holds the registry and hot state across requests. On AgentCore the
  equivalent is the Gateway interceptor Lambdas, packaged as container images
  (`lambda/Dockerfile`).
- State: stateful controls covering drift, circuit, cost, and rate externalize to
  DynamoDB via `governance/shared/state.py` (`DynamoDbState`), mirroring the audit
  ledger's DynamoDB backend.

## Sources

- [Amazon Bedrock AgentCore — Overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [Policy in Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [Apply fine-grained access control with Bedrock AgentCore Gateway interceptors](https://aws.amazon.com/blogs/machine-learning/apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/)
- [Introducing Amazon Bedrock AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-agentcore-gateway-transforming-enterprise-ai-agent-tool-development/)
- [Amazon Bedrock AgentCore Gateway — secure AI gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html)
- [Amazon Bedrock AgentCore documentation](https://docs.aws.amazon.com/bedrock-agentcore/)
- [Amazon Bedrock AgentCore FAQs](https://aws.amazon.com/bedrock/agentcore/faqs/)
