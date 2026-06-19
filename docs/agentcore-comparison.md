# Bedrock AgentCore — overlap, integration, and differentiation

Research note comparing this governance platform with Amazon Bedrock AgentCore,
to decide where to plug into AgentCore and what this framework should keep
owning. Reflects AgentCore as documented in June 2026 (it changes quickly —
re-verify against the live docs before acting). Sources are listed at the end.

## TL;DR

AgentCore has, since its 2025 launch, grown into a broad managed platform that
**natively provides much of the *mechanism* this framework was hand-building**:
a secure gateway as the single entry point for tool/agent/LLM traffic, request/
response interceptors, a deterministic out-of-agent policy engine (Cedar), agent
identity, OTel observability, and a governed registry. The honest conclusion:

- **Do not compete with AgentCore on plumbing.** On AWS, AgentCore Gateway +
  interceptors + Policy replace our hand-built API Gateway → Lambda chokepoint
  and most of `policy_registry`/capability-authz.
- **Our durable value is the governance *content* and portability**, not the
  interception mechanism: the specific layered guard logic (prompt-injection,
  credential/PII redaction, classification-aware data FGAC masking/row-filter,
  behavioural/data-access drift, reasoning-step validation, tamper-evident
  hash-chained audit) and the fact that the same governance runs on Azure and
  GCP, not only AWS.
- **Best play: deploy our enforcement *as* AgentCore interceptors** (the request/
  response Lambdas) and express coarse authorization as AgentCore Policy, while
  keeping the rich guard library and the non-AWS bindings as our own.

## AgentCore component inventory (June 2026)

| Service | What it is |
|---|---|
| Runtime | Serverless, session-isolated host for agents/tools on any framework (LangGraph, CrewAI, Strands, …) and protocol (MCP, A2A). |
| Harness | Managed agent loop (model + prompt + tools in one API call), isolated microVM per session. |
| Gateway | Single secure entry point for agentic traffic; turns APIs/Lambda/services into MCP tools; OAuth inbound/outbound. |
| Gateway interceptors | Custom request/response Lambdas at the gateway for access control, redaction, tool-list filtering, schema translation. |
| Policy | Deterministic authorization engine (Cedar, or natural-language → Cedar) that intercepts every tool call before execution. |
| Identity | Agent identity/auth, token vault; integrates Cognito, Okta, Entra ID, Auth0, any IdP. |
| Memory | Managed short- and long-term agent memory. |
| Observability | OTel-based tracing/metrics/dashboards (CloudWatch). |
| Evaluations | Automated agent/tool quality assessment over sessions/traces/spans. |
| Registry | Governed catalog of agents/MCP servers/tools with publish-review-approve workflow. |
| Code Interpreter / Browser | Sandboxed code execution; managed cloud browser. |
| Payments / Optimization | x402 agent micropayments; A/B-tested config optimization. |

## Overlap map — AgentCore ↔ this framework

| Concern | AgentCore | This framework | Assessment |
|---|---|---|---|
| Agent identity (NHI) | **Identity** (OAuth, token vault, IdP federation) | `core/nhi_registry.py` (agent-type → cloud principal) | AgentCore is more mature on AWS. Ours is portable + simpler. |
| Egress / single chokepoint | **Gateway** (managed, MCP, OAuth) | `bedrock_proxy` + API Gateway IaC | AgentCore supersedes our hand-built plumbing on AWS. |
| Out-of-process enforcement point | **Gateway interceptors** (request/response Lambdas) | `governance/remote/*` proxies | Direct analog. Our enforcement should become the interceptor *payload*. |
| Authorization (who calls what tool, conditions) | **Policy** (Cedar, NL authoring, automated reasoning, fail-closed, outside agent code) | `policy_registry` + capability guard + A2A authz | Strong overlap; AgentCore is arguably ahead (Cedar + reasoning). Adopt it for authz. |
| Observability | **Observability** (OTel/CloudWatch) | OTel spans + audit | Overlap; emit to AgentCore Observability. |
| Eval / red-team | **Evaluations** | `governance/ops` (evals, replay, adversarial) | Overlap; complementary. |
| Tool/agent governance catalog | **Registry** (publish-review-approve) | `policy_registry` + CODEOWNERS workflow | Overlap on AWS. |
| Agent hosting | **Runtime / Harness** | `payload_agents/*` on any framework | Complementary — our agents can run on Runtime. |
| Memory / Browser / Code Interp / Payments | native services | not provided | Complementary; no conflict. |

## What this framework provides over and above AgentCore

Stated conservatively — AgentCore covers more than it did at launch, so the
differentiation is narrower than before but real:

1. **Cloud and provider portability.** AgentCore is AWS-only. This framework runs
   the *same* governance across Azure (APIM/AOAI/Entra), GCP (Apigee/Vertex), and
   AWS (Bedrock). For multi-cloud or non-AWS estates, AgentCore is not an option;
   this is the strongest durable differentiator.

2. **Governance *content* AgentCore Policy does not express.** AgentCore Policy is
   an *authorization* engine (Cedar: identity + tool-input conditions). It does
   not itself provide the control *logic* for:
   - prompt-injection detection (content analysis, not authz);
   - credential/PII detection and redaction (you supply the logic to an interceptor);
   - **data-layer FGAC**: classification-aware column masking and row-level
     filtering over a data-label catalog (ABAC on data sensitivity), with
     store-side pushdown;
   - **behavioural / data-access drift** detection (stateful anomaly detection);
   - **reasoning-step validation** and CoT/CoVe capture with mandatory redaction;
   - **rogue / capability-escalation** detection.
   These are the `agent_os` / `agent_sre` / `agentmesh` bindings. AgentCore gives
   the *interception mechanism*; this framework supplies the *control content*
   that runs inside it.

3. **Tamper-evident audit.** AgentCore logs decisions to CloudWatch. This
   framework adds a hash-chained ledger with explicit tamper detection — evidence
   integrity, not just log retention.

4. **Trust-but-verify composition across in-process + boundary.** The same guard
   library runs in-process (fast, defense-in-depth) and is re-verified at the
   boundary. AgentCore Policy enforces authz at the boundary well; the breadth of
   non-authz controls re-verified is ours.

Note where AgentCore is genuinely ahead: deterministic Cedar policy with
natural-language authoring and automated reasoning to flag overly-permissive/
unsatisfiable rules, managed OAuth identity, and zero-ops scaling. We should
adopt these rather than reimplement them.

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

- **Coarse authorization → AgentCore Policy (Cedar).** Capability allow/deny and
  A2A recipient rules map cleanly to Cedar; let the managed engine own them.
- **Rich controls → Gateway interceptors.** Package `governance/shared` +
  `governance/remote` as the request/response interceptor Lambdas. This is the
  same code from the reorg, deployed into AgentCore's interception points instead
  of our own API Gateway.
- **Data FGAC → a tool fronted by Gateway**, with the masking engine in the
  interceptor/tool (Cedar gates access; our engine masks/filters the rows).
- **Identity → AgentCore Identity**; keep `nhi_registry` as the portable
  abstraction with an AgentCore-backed implementation on AWS.
- **Stateful controls → DynamoDB** (drift baselines, circuit-breaker counters,
  cost accumulators, rate-limit windows); the audit ledger already has a DynamoDB
  backend.

Off AWS, the same `governance/{shared,remote}` runs behind APIM (Azure) or Apigee
(GCP) with no AWS dependency — which is the reason to keep the enforcement library
independent of AgentCore.

## Implications for the runtime decision (Lambda vs daemon)

- We chose Lambda originally because the pre-existing path was API Gateway →
  Lambda → Bedrock: pay-per-use, no servers, scale-to-zero. Limits: cold starts
  (worse once the `agent_os`/`agent_sre`/`agentmesh` toolkit + numpy are bundled),
  a 15-minute cap, and statelessness (the reason stateful controls need an
  external store).
- **If we go AgentCore-native:** there is no separate daemon to run — enforcement
  is deployed as Gateway **interceptor Lambdas**, agents run on **Runtime**, and
  the toolkit-bundle weight is handled with container images. State → DynamoDB.
- **If we stay AgentCore-independent:** a long-lived **Fargate/ECS** service
  (FastAPI behind ALB/API Gateway) is a better fit than Lambda for the
  full-superset trust-but-verify model — the heavy toolkit stays warm in memory,
  the policy registry and in-memory hot state are held across requests, and
  DynamoDB is the durable backing store. This removes per-request cold-start and
  re-init cost.

Recommendation: target **AgentCore Gateway interceptors + Policy on AWS**, and a
**Fargate daemon** as the portable, non-AWS deployment of the same enforcement
library. Avoid plain per-request Lambda for the full enforcement service.

## Implementation status (in this repo)

The integration above is implemented as an offline-verified spike:

| Piece | Code | Verified |
|---|---|---|
| Cedar generation from the registry | `governance/agentcore/cedar_export.py` | `tests/test_agentcore.py` |
| Gateway request interceptor (input + tool-plan) | `cloud_adapters/aws/agentcore/request_interceptor.py` | `tests/test_agentcore.py` |
| Gateway response interceptor (redaction + tool-list filter) | `cloud_adapters/aws/agentcore/response_interceptor.py` | `tests/test_agentcore.py` |
| NHI → AgentCore Identity | `cloud_adapters/aws/agentcore/identity.py` | imports cleanly without the SDK |
| Deploy steps | `cloud_adapters/aws/agentcore/README.md` | documented, not CI-run |

The interceptors and the AWS chokepoints share one enforcement library
(`governance/remote` over `governance/shared/enforcement`), so AWS-native and
off-AWS deployments run identical controls. What is *not* in the repo: a live
AgentCore deploy (account + SDK) and the IAM/Gateway wiring, which are operational
steps.

## Runtime decision (recorded)

- **Lambda vs daemon:** Lambda was inherited from the original API Gateway →
  Bedrock path. For the full trust-but-verify enforcement service the better fit
  is a long-lived **Fargate/ECS** daemon (toolkit warm in memory, registry +
  hot state held across requests); on AgentCore the equivalent is the Gateway
  **interceptor** Lambdas, packaged as container images (`lambda/Dockerfile`).
- **State:** stateful controls (drift/circuit/cost/rate) externalize to
  **DynamoDB** via `governance/shared/state.py` (`DynamoDbState`), mirroring the
  audit ledger's DynamoDB backend.

## Sources

- [Amazon Bedrock AgentCore — Overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [Policy in Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [Apply fine-grained access control with Bedrock AgentCore Gateway interceptors](https://aws.amazon.com/blogs/machine-learning/apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/)
- [Introducing Amazon Bedrock AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-agentcore-gateway-transforming-enterprise-ai-agent-tool-development/)
- [Amazon Bedrock AgentCore Gateway — secure AI gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html)
- [Amazon Bedrock AgentCore documentation](https://docs.aws.amazon.com/bedrock-agentcore/)
- [Amazon Bedrock AgentCore FAQs](https://aws.amazon.com/bedrock/agentcore/faqs/)
