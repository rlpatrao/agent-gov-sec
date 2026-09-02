# Microsoft Agent Framework — overlap, integration, and differentiation

This research note compares the Galaxy Agentic Governance Platform with the
Microsoft Agent Framework (MAF) and the Azure AI Foundry Agent Service, in order
to determine where the platform should integrate with the Azure-native offering
and which responsibilities the platform should retain. It is the Azure analogue
of the AWS `agentcore-comparison.md` note. The description reflects MAF and
Foundry as documented in mid-2026; because both change quickly, readers should
re-verify against the live documentation before acting. Sources are listed at the
end of the document.

## TL;DR

The Microsoft Agent Framework is the Azure-native agent framework — the
convergence of Semantic Kernel and AutoGen — and the Azure AI Foundry Agent
Service provides managed agent hosting on top of it. Together they natively
provide much of the mechanism this platform composes: an agent runtime with
threads and tool calling, a middleware pipeline, `gen_ai.*` OpenTelemetry
observability, model access through Azure OpenAI, Azure AI Content Safety, and
Entra identity.

The analysis supports the following conclusions:

- The platform should not compete with MAF on plumbing. On Azure, the MAF
  middleware pipeline is the extension point the governance stack runs inside,
  and Foundry supplies the runtime, threads, model catalogue, and tracing sink.
- The durable value of the platform is its governance content and portability
  rather than the middleware mechanism. That content comprises the layered guard
  logic — prompt-injection detection, credential and PII redaction,
  classification-aware data FGAC masking and row-filtering, behavioural and
  data-access drift detection, reasoning-step validation, and A2A recipient
  authorization — together with the design that runs the same governance on
  cloud bindings other than Azure through the cloud-neutral core.
- The recommended approach is to run the governance personas as MAF agents with
  the governance middleware stack (Method 2) and/or to route egress through the
  APIM → Function chokepoint (Method 1), while retaining the guard library and
  the non-Azure bindings as platform responsibilities.

One honest difference from the AWS side is stated up front: Azure has no managed
policy engine analogous to AgentCore's Cedar service. On Azure, coarse
authorization (which tool, which recipient) is enforced in-process by the MAF
policy, capability, and A2A middlewares and re-checked at the APIM edge. There is
no managed Cedar engine, and none is invented here.

## MAF / Foundry component inventory (mid-2026)

| Component | What it is |
|---|---|
| Agent Framework (MAF) | The Azure-native agent framework; `Agent` class, threads, tool/function calling. Successor and convergence of Semantic Kernel and AutoGen. |
| Middleware pipeline | Agent, chat, and function middleware hooks (`AgentMiddleware` / `ChatMiddleware` / `FunctionMiddleware`) with `MiddlewareTermination` to short-circuit a call. |
| Foundry Agent Service | Managed hosting for agents — runtime, threads, tool calling, and the connected-tools surface. |
| Model catalogue | Azure OpenAI plus other served models, reached through `FoundryChatClient`. |
| Observability | `gen_ai.*` OTel semantic-convention spans via `configure_otel_providers`, emitted to Application Insights ("Agents (preview)" dashboard). |
| Azure AI Content Safety | Managed content filtering (hate/violence/self-harm/sexual categories, jailbreak/prompt-shield). |
| Entra identity | Microsoft Entra ID principals; User-Assigned Managed Identities and Workload Identity federation. |
| Connected tools / MCP | Tool connection surface, including a Model Context Protocol path. |

There is no managed authorization engine in this inventory. That absence is the
principal structural difference from AWS AgentCore, where Policy (Cedar) is a
managed service.

## Overlap map — MAF/Foundry ↔ this platform

| Concern | MAF / Foundry | This platform | Assessment |
|---|---|---|---|
| Agent runtime + threads | MAF `Agent` · Foundry Agent Service | `payload_agents/*` on any framework | Complementary — the personas run as MAF agents inline in the host. |
| Middleware / interception | Middleware pipeline (agent/chat/function hooks) | MAF guard middlewares (`framework_adapters/maf/`) | Direct analog. The governance stack is composed as MAF middlewares. |
| Authorization (who calls what tool / which recipient) | not a managed service | `agent_os` policy/capability/rogue middlewares + A2A allow-list + APIM edge check | No Azure managed engine. Enforced in-process, re-checked at APIM. |
| Content safety | Azure AI Content Safety | prompt-injection · credential/PII guards | Complementary, layered; the platform adds injection and credential/PII logic. |
| Observability | `gen_ai.*` OTel → App Insights | OTel spans + hash-chained ledger | Overlap on tracing; the ledger adds tamper-evidence. |
| Model access | Azure OpenAI via `FoundryChatClient` | LLM-egress chokepoint (APIM/Function) | Complementary; the platform pins the deployment and holds the key at the edge. |
| Identity | Entra ID | `core/nhi_registry` → Entra Managed Identity | The platform binds per-agent Managed Identities as NHIs. |
| Data governance | Microsoft Purview (catalogue) | FGAC mediator + Azure SQL / Synapse pushdown | Complementary; Purview can populate the classification catalogue the FGAC engine consumes. |

## What this platform provides over and above native MAF

MAF and Foundry cover the runtime, the pipeline mechanism, tracing, and managed
content safety. The platform provides the following capabilities over and above
that native offering:

1. Cloud and provider portability. MAF operates on Azure only. The same
   `GuardPipeline` runs over a cloud-neutral core, so the Azure (MAF) binding is
   one of several — the identical governance runs on AWS and GCP. For multi-cloud
   or non-Azure estates, MAF is not an option, which makes portability the
   strongest durable differentiator.

2. Governance content the framework does not ship as a policy surface. The MAF
   middleware pipeline is a mechanism; it does not itself supply control logic for
   the following concerns:
   - Prompt-injection detection, which is content analysis over a seven-vector
     taxonomy rather than a managed content-safety category
   - Credential and PII detection and redaction, supplied to a middleware and to
     the boundary Function
   - Data-layer FGAC: classification-aware column masking and row-level filtering
     over a data-label catalogue (ABAC on data sensitivity), with Azure SQL /
     Synapse store-side pushdown and Row-Level Security registration
   - Behavioural and data-access drift detection, implemented as stateful anomaly
     detection
   - Reasoning-step validation and CoT/CoVe capture with mandatory redaction
   - A2A recipient authorization between agents

   These controls are supplied by the `agent_os`, `agent_sre`, and `agentmesh`
   toolkit; `build_governance_stack` composes the detectors as MAF middlewares.
   MAF provides the pipeline, while the platform supplies the control content that
   runs inside it.

3. Tamper-evident audit. MAF and Foundry emit spans to Application Insights. The
   platform adds a hash-chained ledger (PostgreSQL `trace_ledger`, SHA-256, each
   entry hashing the previous) with explicit tamper detection, which provides
   evidence integrity rather than log retention alone.

4. Trust-but-verify composition across in-process and boundary enforcement. The
   same `EnforcementSession` runs in-process as MAF middleware for
   defense-in-depth and is re-run at an out-of-process chokepoint — the Function
   `llm` / `data` / `a2a` proxies — under a separate Managed Identity. An agent
   that bypasses its in-process middlewares is still stopped at the boundary. The
   two paths share one enforcement object, so they cannot drift.

MAF is ahead in several areas, which the platform consumes rather than
reimplements:

- Managed agent hosting, threads, and tool calling (Foundry Agent Service)
- Managed content safety (Azure AI Content Safety), layered beneath the
  platform's injection and credential/PII guards
- The `gen_ai.*` OTel provider setup (`configure_otel_providers`), which the
  runtime adapter delegates to so spans reach the "Agents (preview)" dashboard
- Entra identity and Workload Identity federation

## Recommended integration architecture (on Azure)

```
  MAF Agent (Foundry Agent Service / Container Apps / AKS)
     │  middleware = build_governance_stack(...)
     ├── prompt-injection ─┐
     ├── credential redact  │  in-process guard middlewares
     ├── context budget     │  (agent_os detectors, fail-fast order)
     ├── policy / capability / rogue ─┘  (coarse authz — no managed Cedar)
     └── model call ──► APIM galaxy-<suffix>-apim ──► Function enforce_llm ──► Azure OpenAI
                          (subscription key + edge re-check)   (EnforcementSession re-run)
  Data access  ──► Function enforce_data ──► Azure SQL / Synapse (FGAC pushdown + RLS)
  A2A dispatch ──► Function enforce_a2a  (recipient allow-list)
  Identity     ◄─ NHI registry → Entra User-Assigned Managed Identity (galaxy-<persona>-mi)
  Observability ◄─ MAF gen_ai.* OTel → App Insights + hash-chained Postgres ledger
```

The integration assigns responsibilities as follows:

- Coarse authorization is enforced in-process by the MAF policy, capability, and
  rogue middlewares (`create_governance_middleware`, reading the policy YAML set)
  and re-checked at the APIM edge. There is no managed engine to delegate to, so
  the authoritative re-check happens at the Function boundary rather than in a
  Cedar service.
- Rich controls run as MAF guard middlewares in-process and are re-run at the
  boundary. `galaxy_gov/shared/enforcement` and `galaxy_gov/remote` supply the
  single `EnforcementSession`; the MAF guards
  (`framework_adapters/maf/guards/`) wrap the same detectors for the in-process
  path.
- Data FGAC is expressed through the data proxy. `AzureSqlFgacEnforcer` rewrites
  reads as scoped Azure SQL / Synapse T-SQL (projecting allowed columns, masking
  masked columns, filtering rows) and can register Row-Level Security so the store
  enforces the scope for any client on the agent's principal.
- Identity is bound to Entra. `nhi_registry` is retained as the portable
  abstraction, with `AzureIdentityProvider` resolving each persona's
  User-Assigned Managed Identity.
- Observability lets MAF own the `TracerProvider` (`MafRuntimeAdapter`) so the
  `gen_ai.*` spans reach App Insights, while the hash-chained ledger records the
  audit trail in parallel.

Off Azure, the same `governance/{shared,remote}` modules run behind an AWS or GCP
binding with no Azure dependency. This portability is the reason to keep the
enforcement library independent of MAF.

## Implications for the runtime decision (inline vs managed node)

MAF agents run inline in the host application — Container Apps, AKS, or the
Foundry Agent Service — rather than as a separate managed runtime node like AWS
AgentCore Runtime. There is no distinct "runtime" resource to provision on Azure;
the agent is an object constructed in the host process with the governance
middleware list attached. This is an architectural difference from the AWS side,
where each persona is a discrete AgentCore Runtime observable in the console.

Consequences of the inline model:

- The governance stack is attached at construction (`Agent(middleware=...)`), so
  in-process enforcement is the default path and requires no separate deployment.
- The out-of-process authority is the APIM → Function chokepoint, not a managed
  agent host. The Function re-runs the `EnforcementSession` under its own Managed
  Identity, holds the Azure OpenAI key from Key Vault, and pins the deployment
  id server-side, so the agent never holds the model key.
- Per-persona fan-out is expressed as Container Apps Jobs (`aca_jobs.bicep`), one
  job per persona under its own Managed Identity, started by the orchestrator —
  the Azure counterpart of one Runtime per persona on AWS.

## Implementation status (in this repo)

The MAF integration is implemented; the in-process path is live and tested. The
following table records each piece and its code location:

| Piece | Code | Status |
|---|---|---|
| MAF governance middleware stack | `framework_adapters/maf/middleware.py` (`build_governance_stack`) | Live — composes agent_os detectors as MAF middlewares |
| MAF guard middlewares | `framework_adapters/maf/guards/{prompt_injection,credential_redactor,context_budget}.py` | Live |
| MAF runtime adapter | `framework_adapters/maf/runtime.py` (`MafRuntimeAdapter`) | Built — MAF owns the OTel provider |
| Hash-chain ledger | `cloud_adapters/azure/audit.py` (`PostgresHashChainBackend`) | Live — Postgres `trace_ledger`, SHA-256 |
| Secrets | `cloud_adapters/azure/secrets.py` (`TokenProvider`) | Live — Key Vault via Managed Identity |
| Identity | `cloud_adapters/azure/identity.py` (`AzureIdentityProvider`) | Live — Entra Managed Identity |
| Tracing | `cloud_adapters/azure/tracing.py` (`AzureTraceExporterFactory`) | Live — App Insights exporter |
| FGAC enforcer | `cloud_adapters/azure/data_fgac.py` (`AzureSqlFgacEnforcer`) | Built — Azure SQL / Synapse pushdown + RLS |
| Function chokepoints | `cloud_adapters/azure/infra/functions/{llm_proxy,data_proxy,a2a_broker}.py` | Built, not deployed |
| APIM proxy | `cloud_adapters/azure/infra/main.bicep` | Reference IaC — deploy per subscription |
| Container Apps Jobs | `cloud_adapters/azure/infra/aca_jobs.bicep` | Reference IaC — one job per persona |

The in-process path is live and verified: the full suite of 279 tests passes with
`azure` as the default provider (`CLOUD_PROVIDER=azure`), and
`scripts/demo_agents.py --azure --extended` runs the control matrix over the three
personas against live Azure OpenAI. The MAF middleware stack, the guard
middlewares, the Postgres ledger, Key Vault secrets, Entra identity, and App
Insights tracing all run on this path.

The out-of-process topology is reference infrastructure. The APIM proxy and the
Container Apps Jobs are reference Bicep, and the Function chokepoints are built
but not deployed. No live governance-persona deployment currently runs on Azure;
the live persona deployment is AWS AgentCore (us-east-2). On Azure the governance
runs in-process today, and the boundary topology is provisioned per subscription
when a managed deployment is wanted.

## Runtime decision (recorded)

- Inline versus managed node: MAF agents run inline in the host process with the
  governance middleware attached at construction, so the in-process path needs no
  separate runtime resource. The out-of-process authority is the APIM → Function
  chokepoint, not a managed agent host.
- Authorization: there is no managed policy engine on Azure. Coarse authorization
  is made in-process by the MAF policy, capability, and A2A middlewares and
  re-checked at the APIM edge, and the boundary Function re-runs the same
  `EnforcementSession`. This is the deliberate Azure counterpart to AgentCore
  Policy on AWS.
- State: stateful controls (drift baselines, circuit, cost, rate) hold state in
  process for the in-process path; a durable store is wired when the boundary
  topology is deployed.

## Sources

- [Microsoft Agent Framework — overview](https://learn.microsoft.com/en-us/agent-framework/)
- [Azure AI Foundry Agent Service documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/)
- [Agent Framework middleware](https://learn.microsoft.com/en-us/agent-framework/user-guide/agents/middleware)
- [Observability in the Agent Framework](https://learn.microsoft.com/en-us/agent-framework/user-guide/observability/)
- [Azure AI Content Safety documentation](https://learn.microsoft.com/en-us/azure/ai-services/content-safety/)
- [Microsoft Entra ID — managed identities](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/overview)
- [Azure SQL Row-Level Security](https://learn.microsoft.com/en-us/sql/relational-databases/security/row-level-security)
- [Microsoft Purview data governance](https://learn.microsoft.com/en-us/purview/)
