# Services & technology inventory

**Last updated:** 2026-07-01
**Scope:** This document inventories the external services, Python libraries, governance policy files, and environment variables on which this repository depends when run on Azure. For each item, it records the function performed, the location where it is configured, and its current status. This document covers the Azure stack of the documentation set. Azure is the default cloud binding (`CLOUD_PROVIDER=azure`); AWS and GCP are maintained as separate documentation stacks (`../aws/`, `../gcp/`), and the cloud-neutral platform reference is in `../shared/`.

For the system design and sequence diagrams, see [architecture.md](architecture.md).

> **Repo scope.** This repository implements the **Galaxy Agentic Governance Platform**, a runtime governance and security layer (`core/`, `galaxy_gov/`, `core/a2a/`) built on the `agent_os` / `agent_sre` / `agentmesh` packages and, on Azure, the Microsoft Agent Framework (MAF). It additionally provides a **minimal demonstration payload** (`payload_agents/`) comprising three personas (**FinOps**, **Auditor**, **Rogue**). An earlier multi-agent SDLC product that ran on Azure Container Apps has been moved to a **local-only, gitignored `archive/`** and is **not part of this repository**. Where this document references that product, it is labeled **(archived)** for context.

---

## 1. Azure resource topology

This repository supports two Azure enforcement methods. Both methods consume the same policy registry (`galaxy_gov/policy_export.py`) and run the full demo matrix. Method 1 routes agents through an API Management egress edge in front of an Azure Function and Azure OpenAI, and is provided as reference Bicep under [`cloud_adapters/azure/infra/main.bicep`](../../cloud_adapters/azure/infra/main.bicep). Method 2 composes the governance controls as a Microsoft Agent Framework middleware stack; the in-process middleware runs live, and managed hosting on Azure AI Foundry Agent Service is a reference deploy step. No live governance-persona deployment currently runs on Azure — on Azure the governance runs in-process today, and the out-of-process topology is reference IaC. The live reference deployment for the three personas is AWS AgentCore (us-east-2).

### 1.1 Method 1 — APIM proxy (reference Bicep)

> Provisioned by [`cloud_adapters/azure/infra/main.bicep`](../../cloud_adapters/azure/infra/main.bicep). Resource names are prefixed `galaxy-<suffix>`; all resources are tagged `project: galaxy-rp`. Deploy against your own subscription and region; Azure OpenAI model access must be enabled for the tenant first.

| # | Resource | What it does | Status | Where it touches code |
|---|---|---|---|---|
| 1 | API Management (`galaxy-<suffix>-apim`, Consumption; API `aoai-egress`, path `openai`) | The managed LLM-egress edge in front of Azure OpenAI. Validates the `Ocp-Apim-Subscription-Key` and required headers, applies a rate limit, and injects the Azure OpenAI key from a Key-Vault-backed named value so the key never reaches callers. | Reference Bicep | [cloud_adapters/azure/gateway.py](../../cloud_adapters/azure/gateway.py) — `AzureLLMGateway` (`apim` mode); [cloud_adapters/azure/infra/apim-policy.xml](../../cloud_adapters/azure/infra/apim-policy.xml) |
| 2 | Function App (`galaxy-<suffix>-func`, Linux, Python 3.12) | Hosts the out-of-process chokepoints. Routes `POST /api/llm` → `enforce_llm`, `POST /api/data` → `enforce_data`, `POST /api/a2a` → `enforce_a2a`. The deployment id is pinned server-side (`AZURE_OPENAI_DEPLOYMENT`, e.g. `gpt-4o`). | Reference Bicep (functions built, not deployed) | [cloud_adapters/azure/infra/functions/](../../cloud_adapters/azure/infra/functions/) |
| 3 | Azure OpenAI | Hosts the model behind the APIM edge (`AZURE_OPENAI_DEPLOYMENT`, e.g. `gpt-4o`). The deployment id is pinned server-side; a request-body `model` is ignored. | Reference Bicep | [cloud_adapters/azure/infra/functions/llm_proxy.py](../../cloud_adapters/azure/infra/functions/llm_proxy.py) — `_call_aoai` |
| 4 | Entra User-Assigned Managed Identities (`galaxy-<persona>-mi`) | Per-agent identity (the NHI). One managed identity per persona; its `clientId` is the NHI principal id and flows into the relevant `NHI_CLIENT_ID_*` env var. | Reference Bicep | [cloud_adapters/azure/identity.py](../../cloud_adapters/azure/identity.py) — `AzureIdentityProvider`; [core/nhi_registry.py](../../core/nhi_registry.py) reads `NHI_CLIENT_ID_*` from env |
| 5 | Key Vault (`galaxy<suffix>kv`) | Stores `azure-openai-key`, `apim-subscription-key`, `postgres-password`, and `appinsights-connection-string`. Fetched with the Function App's managed identity through a 5-minute-TTL cache. | Reference Bicep | [cloud_adapters/azure/secrets.py](../../cloud_adapters/azure/secrets.py) — `TokenProvider` |
| 6 | PostgreSQL Flexible Server (`galaxy-<suffix>-pg`, database `ledger`, table `trace_ledger`) | Persistent hash-chained `trace_ledger` archive — SHA-256 chain, append-only (no `UPDATE`/`DELETE` granted to the app user). Survives restarts; queryable for compliance. When unreachable the chain is still built and verified in memory (stdout mode). | Reference Bicep | [cloud_adapters/azure/audit.py](../../cloud_adapters/azure/audit.py) — `PostgresHashChainBackend`; schema [cloud_adapters/azure/infra/ledger_schema.sql](../../cloud_adapters/azure/infra/ledger_schema.sql) |
| 7 | Log Analytics (`galaxy-<suffix>-law`) + Application Insights (`galaxy-<suffix>-ai`) | OTel span sink. The app exports directly to Application Insights (no collector); Log Analytics is queried with KQL. | Reference Bicep | [cloud_adapters/azure/tracing.py](../../cloud_adapters/azure/tracing.py) — `AzureTraceExporterFactory` |
| 8 | Storage account (`galaxy<suffix>sa`) | Functions backing store; also backs the Azure Files share mounted by the Container Apps jobs (Method 2). | Reference Bicep | [cloud_adapters/azure/infra/aca_jobs.bicep](../../cloud_adapters/azure/infra/aca_jobs.bicep) |

The out-of-process chokepoints are the Function decision handlers `enforce_llm` / `enforce_data` / `enforce_a2a`. Each re-runs the same `EnforcementSession` under the Function App's own managed identity, so an agent that bypasses its in-process guards is still stopped. The handlers are pure decision functions (transport-agnostic, unit-tested without the Functions runtime); `function_app.py` adapts an Azure Functions `HttpRequest` onto them. These functions are **built, not deployed**.

Live IDs and endpoints are scrubbed and kept out of the repository. Populate them into a local `.env` from [.env.example](../../.env.example); the values are obtained from the Bicep deployment outputs (`apimEndpoint`, `keyVaultUrl`, `ledgerHost`, `appInsightsConnectionString`, `personaClientIds`).

### 1.2 Method 2 — Microsoft Agent Framework (in-process live; managed hosting reference)

> The MAF governance middleware stack runs in-process (live). Managed hosting on Azure AI Foundry Agent Service is a reference deploy step; the per-persona Container Apps Jobs shape is reference Bicep under [`cloud_adapters/azure/infra/aca_jobs.bicep`](../../cloud_adapters/azure/infra/aca_jobs.bicep). Authorization here is in-process: the MAF policy / capability / rogue middlewares from `agent_os` make the decision. Azure has no managed policy-engine analogue to AgentCore's Cedar engine.

| # | Resource | What it does | Status | Where it touches code |
|---|---|---|---|---|
| 1 | MAF governance middleware stack (`build_governance_stack`) | Composes the `agent_os` governance primitives into a Microsoft Agent Framework middleware list, ordered to fail fast on cheap checks first: prompt-injection → credential redactor → context budget → audit → policy → capability → rogue detection. Passed to `Agent(middleware=...)`. | Live (in-process) | [cloud_adapters/azure/maf/middleware.py](../../cloud_adapters/azure/maf/middleware.py) |
| 2 | MAF guard middlewares | The Azure wrappers around the shared guard primitives — prompt-injection, credential redactor, context budget. | Live (in-process) | [cloud_adapters/azure/maf/guards/](../../cloud_adapters/azure/maf/guards/) |
| 3 | MAF runtime adapter (`MafRuntimeAdapter`) | Lets MAF own the OTel `TracerProvider` (via `configure_otel_providers`) so its `gen_ai.*` spans reach the Azure "Agents (preview)" dashboard. Falls back to the agnostic provider when MAF is not installed. | Live (in-process) | [cloud_adapters/azure/maf/runtime.py](../../cloud_adapters/azure/maf/runtime.py) |
| 4 | Container Apps Jobs (`galaxy-<persona>-job`) | One manual-trigger job per persona, each under its own User-Assigned Managed Identity (the persona's NHI), for the fan-out shape. Artifacts flow through the Azure Files share mounted at `/data`. Started by `submit_agent_job`. | Reference Bicep | [cloud_adapters/azure/orchestrator.py](../../cloud_adapters/azure/orchestrator.py) — `submit_agent_job`; [cloud_adapters/azure/infra/aca_jobs.bicep](../../cloud_adapters/azure/infra/aca_jobs.bicep) |
| 5 | Postgres hash-chain ledger + Application Insights | Shared with Method 1 — the same `PostgresHashChainBackend` and Azure Monitor span sink. | Reference Bicep / live | [cloud_adapters/azure/audit.py](../../cloud_adapters/azure/audit.py), [cloud_adapters/azure/tracing.py](../../cloud_adapters/azure/tracing.py) |

The per-persona managed identities are `galaxy-finops-mi` / `galaxy-auditor-mi` / `galaxy-rogue-mi`. These identities serve as the NHIs bound to the MAF host and the Container Apps job executions.

---

## 2. External services

| Service | Used today? | Notes |
|---|---|---|
| **Azure OpenAI** (chat completions) | Every live LLM call | The agents reach Azure OpenAI through the APIM edge / LLM proxy, not the endpoint directly. Deployment selected via `AZURE_OPENAI_DEPLOYMENT` and pinned server-side. **Not used by the offline demo.** |
| **API Management → Function → Azure OpenAI** | Method 1 live LLM calls | The governed egress path. APIM validates the subscription key and injects the AOAI key at the edge; the agent carries only the `Ocp-Apim-Subscription-Key`. |
| **Azure Key Vault** | Live runs | Stores the AOAI key, APIM subscription key, Postgres password, and App Insights connection string, read by `TokenProvider` with a 5-minute-TTL cache. |
| **Azure Monitor / Application Insights** | Live runs | OTel spans export directly to Application Insights when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set; Log Analytics is queried with KQL. |
| **Microsoft Entra ID** | Live runs | Per-agent User-Assigned Managed Identities are the NHIs; `AzureIdentityProvider` resolves a `ManagedIdentityCredential` scoped to the persona's `clientId`. |
| **PostgreSQL Flexible Server** | Live runs | Backs the append-only `trace_ledger` hash chain (`PostgresHashChainBackend`). Stdout mode when the DSN or `asyncpg` is absent. |
| **Azure SQL / Synapse Serverless** | FGAC pushdown (reference) | `AzureSqlFgacEnforcer` rewrites reads as scoped T-SQL and registers Row-Level Security predicates; Microsoft Purview can populate the classification catalog (documented seam). |
| **Azure Container Apps Jobs** | Method 2 fan-out (reference) | One job execution per persona, started by `submit_agent_job` under each persona's managed identity. |

---

## 3. Python runtime stack

### 3.1 Governance packages — `agent_os` / `agent_sre` / `agentmesh` (policies + audit + circuit breaker)

| Package | Version | Role |
|---|---|---|
| `agent-os-kernel` | `>=3.7.0` | The runtime governance engine. Provides `agent_os.policies.PolicyEvaluator`, `agent_os.audit_logger.GovernanceAuditLogger`, `agent_os.circuit_breaker.CircuitBreaker`, `agent_os.prompt_injection.PromptInjectionDetector`, `agent_os.context_budget.ContextScheduler`, and `agent_os.integrations.maf_adapter` (the MAF middleware this repo wraps via `create_governance_middleware`). |
| `agent-sre` | `>=3.7.0` | Provides `agent_sre.anomaly.RogueAgentDetector`, imported by `agent_os.integrations.maf_adapter`, plus the fleet-ops primitives (SLO, evals, replay, SBOM, signing, certification). |
| `agentmesh-platform` | `>=3.7.0` | Required transitively by `agent_os.integrations.maf_adapter` (`from agentmesh.governance import AuditEntry, AuditLog`). Without it, imports fail. |

### 3.2 Azure SDK + MAF — identity, secrets, tracing, ledger, framework (`.[azure]` extra)

This stack is installed via `pip install '.[azure]'`. The agent does not hold the Azure OpenAI key; it reaches Azure OpenAI only through the APIM edge, which injects the key. The Azure SDK imports are guarded so importing an adapter module without the SDK degrades to env-var / no-op mode rather than failing.

| Package | Version | Role |
|---|---|---|
| `agent-framework-core` | `>=1.8.1,<2` | The Microsoft Agent Framework. Provides the `Agent` and middleware surface the guard stack composes onto, and `agent_framework.observability.configure_otel_providers` for the `gen_ai.*` spans. |
| `agent-framework-foundry` | `>=1.8.1` | The Azure AI Foundry integration for MAF (managed hosting path). |
| `azure-identity` | `>=1.19.0` | `DefaultAzureCredential` / `ManagedIdentityCredential` for Key Vault fetches and per-agent NHI resolution (Workload Identity federated tokens in AKS/ACA). |
| `azure-keyvault-secrets` | `>=4.8.0` | `SecretClient` for the AOAI key / APIM sub-key / Postgres password fetch (`TokenProvider`). |
| `azure-monitor-opentelemetry-exporter` | `>=1.0.0b50` | `AzureMonitorTraceExporter` — OTel span export to Application Insights (`AzureTraceExporterFactory`). |
| `asyncpg` | `>=0.29.0` | Async PostgreSQL driver for the `trace_ledger` hash-chain backend (`PostgresHashChainBackend`). |
| `opentelemetry-exporter-otlp-proto-grpc` | `>=1.27.0` | OTLP exporter fallback when Application Insights is not configured. |

### 3.3 Framework axis (orthogonal to the cloud axis)

These frameworks are installed via the framework extras in `pyproject.toml`. Each adapter calls the same shared `GuardPipeline`; selecting a framework does not change which controls run. On Azure the default framework binding is MAF (§3.2); the extras below are optional.

| Package | Version | Role |
|---|---|---|
| `langgraph` / `langchain` / `langchain-openai` | `>=1.0` | The LangGraph adapter (`--framework langgraph`), via the `.[langgraph]` extra. |
| `pydantic-ai-slim` | `>=1.0` | The Pydantic AI adapter, via the `.[pydantic]` extra. |

### 3.4 OpenTelemetry

| Package | Version | Role |
|---|---|---|
| `opentelemetry-api` | `>=1.27.0` | `trace.get_tracer`, span context, propagation. |
| `opentelemetry-sdk` | `>=1.27.0` | `TracerProvider`, `BatchSpanProcessor`, `Resource`. |
| `azure-monitor-opentelemetry-exporter` | `>=1.0.0b50` | Direct span export to Application Insights. |

### 3.5 Storage / config

| Package | Version | Role |
|---|---|---|
| `pydantic` | `>=2.0.0,<3` | Schema validation for `payload_agents/config/*.yaml`. Pinned explicitly so dependency bumps cannot cross the major Pydantic line. |
| `PyYAML` | `>=6.0.1` | Reads YAML config + governance policies. |
| `python-dotenv` | `>=1.0.0` | Loads `.env` for local dev. |

### 3.6 Test

| Package | Version | Role |
|---|---|---|
| `pytest` | `>=8.0.0` | Test runner. |
| `pytest-asyncio` | `>=0.24.0` | `@pytest.mark.asyncio` for the async governance and ledger tests. |

---

## 4. Local tooling

| Tool | Why it's used | Notes |
|---|---|---|
| `python` 3.12+ | Runtime | The Function App and Container Apps jobs pin Python 3.12. |
| `uv` | pip resolver / venv manager | Used for `uv venv` / `uv pip install` / `uv run`. |
| `az` (Azure CLI) | Azure auth + queries for live cloud runs | `az login`; not needed for the offline demo or tests. |
| `az bicep` (0.42+) | Provisions the Azure infra | `az deployment group create -f main.bicep` / `aca_jobs.bicep`. Resources tagged `project: galaxy-rp`. |
| `git` | Source control | — |
| `gh` (GitHub CLI) | GitHub operations | Used as needed. |

---

## 5. Governance policies (YAML on disk)

The `*.yaml` policy packs are loaded by `agent_os.policies.PolicyEvaluator` (or, on Azure, by `create_governance_middleware`) at agent build time. All files in `galaxy_gov/policies/` are loaded automatically, and no manifest is required. The MAF middleware stack ([cloud_adapters/azure/maf/middleware.py](../../cloud_adapters/azure/maf/middleware.py)) wires the policy directory into the toolkit middleware.

| File | What it enforces |
|---|---|
| [galaxy_gov/policies/galaxy-core.yaml](../../galaxy_gov/policies/galaxy-core.yaml) | Prompt-injection regex (OWASP ASI-01) + oversized-prompt gate |
| [galaxy_gov/policies/galaxy-tools.yaml](../../galaxy_gov/policies/galaxy-tools.yaml) | Per-agent tool allow-list. FinOps and Auditor declare their read tools; Rogue ships with `allowed_tools: []`, so every tool it attempts is denied. |
| [galaxy_gov/policies/galaxy-pii.yaml](../../galaxy_gov/policies/galaxy-pii.yaml) | PII rules placeholder — `defaults.action=allow` (no-op) until a PII detector is wired |
| [galaxy_gov/policies/galaxy-ast.yaml](../../galaxy_gov/policies/galaxy-ast.yaml) | **(archived)** AST-agent-specific rules (deny outbound A2A from leaf agent, etc.) |

Two further guard configurations are read by the pre-middleware guards rather than by `PolicyEvaluator`:

| File | What it tunes |
|---|---|
| [cloud_adapters/azure/egress.yaml](../../cloud_adapters/azure/egress.yaml) | Outbound network egress allow-list — the APIM / Azure OpenAI / Key Vault / Application Insights hosts as the only permitted destinations |
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

The offline demo (`scripts/demo_governance.py`) and the test suite require **none** of these variables. The variables below apply to a live `demo_agents.py --azure` run against Azure OpenAI and, where noted, to the reference infrastructure. The endpoints and keys are obtained from the Bicep deployment outputs. For the full template, see [.env.example](../../.env.example).

| Variable | Purpose | Required? | Read at |
|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | The Azure OpenAI resource endpoint (direct-AOAI mode, and the Function proxy's upstream) | Required for live `--azure` when APIM is unset | [cloud_adapters/azure/gateway.py](../../cloud_adapters/azure/gateway.py), [cloud_adapters/azure/infra/functions/llm_proxy.py](../../cloud_adapters/azure/infra/functions/llm_proxy.py) |
| `AZURE_OPENAI_DEPLOYMENT` | The deployment id (e.g. `gpt-4o`), pinned server-side; a request-body `model` is ignored | Required for live `--azure` | [cloud_adapters/azure/infra/functions/llm_proxy.py](../../cloud_adapters/azure/infra/functions/llm_proxy.py) |
| `AZURE_OPENAI_API_VERSION` | API version (use a dated value, e.g. `2025-03-01-preview`) | Optional (code default `2025-03-01-preview`) | [cloud_adapters/azure/infra/functions/llm_proxy.py](../../cloud_adapters/azure/infra/functions/llm_proxy.py) |
| `AZURE_OPENAI_KEY` | The AOAI key, used only for local direct-AOAI dev; in the deployed path the key is injected at the APIM edge and never reaches callers | Optional (Key Vault preferred) | [cloud_adapters/azure/secrets.py](../../cloud_adapters/azure/secrets.py) — `TokenProvider` env fallback |
| `APIM_ENDPOINT` | The API Management gateway URL — when set, all agents route through the APIM egress chokepoint | Optional (APIM mode) | [cloud_adapters/azure/gateway.py](../../cloud_adapters/azure/gateway.py) |
| `APIM_SUBSCRIPTION_KEY` | Local fallback for the `Ocp-Apim-Subscription-Key` (Key Vault `apim-subscription-key` preferred) | Optional (Key Vault preferred) | [cloud_adapters/azure/gateway.py](../../cloud_adapters/azure/gateway.py) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Application Insights connection string; tracing exports directly to Azure Monitor when set, no-op when unset | Optional (recommended for live runs) | [cloud_adapters/azure/tracing.py](../../cloud_adapters/azure/tracing.py) |
| `AZURE_KEY_VAULT_URL` | The Key Vault data-plane URL; when set, `TokenProvider` fetches secrets with the managed identity | Optional (env-var fallback locally) | [cloud_adapters/azure/secrets.py](../../cloud_adapters/azure/secrets.py) |
| `POSTGRES_DSN` | PostgreSQL DSN for the `trace_ledger` hash chain; stdout mode when unset | Optional (recommended for live runs) | [cloud_adapters/azure/audit.py](../../cloud_adapters/azure/audit.py) — `PostgresHashChainBackend.create` |
| `NHI_CLIENT_ID_FINOPS` / `_AUDITOR` / `_ROGUE` | The three personas' NHI principal ids (Entra `clientId`); flow into `agent_id` and every audit row's `nhi_id` | Required for live runs (placeholder OK locally) | [cloud_adapters/azure/identity.py](../../cloud_adapters/azure/identity.py), [core/nhi_registry.py](../../core/nhi_registry.py) |
| `CLOUD_PROVIDER` | Selects the cloud adapter; `azure` is the default binding | Optional (default `azure`) | [core/provider_factory.py](../../core/provider_factory.py) |
| `GALAXY_GAP_*` / `GALAXY_OPS_*` | Toggle the flag-gated controls (26 off by default); truthy = `1`/`true`/`yes`/`on` | Optional (all off by default) | [galaxy_gov/shared/enforcement/flags.py](../../galaxy_gov/shared/enforcement/flags.py) |

Setting `CLOUD_PROVIDER=azure` (the default) selects the Azure adapter, comprising the PostgreSQL ledger, Key Vault secrets, Entra identity, Azure Monitor tracing, and the APIM / Azure OpenAI egress path.

### 6.1 Function chokepoint (Method 1) variables

These variables are set on the Function App by the Bicep deployment and are not part of the local `.env` contract described above. They are listed here for reference.

| Variable | Purpose | Set on |
|---|---|---|
| `GOV_POLICY_REGISTRY_PATH` | Path to the bundled policy registry (`/home/site/wwwroot/agent-controls.json`) the chokepoint functions read | The Function App |
| `AZURE_SQL_CONNECTION_STRING` | ODBC connection string used only to apply Row-Level Security DDL (`AzureSqlFgacEnforcer`, `apply=True`) | Set where FGAC pushdown is applied |
| `AZURE_SQL_SCHEMA` | T-SQL schema qualifier for FGAC pushdown (default `dbo`) | Set where FGAC pushdown is applied |
| `AZURE_SUBSCRIPTION_ID` / `AZURE_RESOURCE_GROUP` / `AZURE_ACA_JOB_PREFIX` | Target subscription, resource group, and job-name prefix for the Container Apps Jobs orchestrator | The orchestrator host (Method 2 fan-out) |

---

## 7. Telemetry attribute vocabulary

This section describes the OTel span and event attributes that flow to Application Insights and Log Analytics. The attributes are drawn from the following sources:

- **GenAI semantic conventions** (emitted by MAF to the Azure "Agents (preview)" dashboard): `gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.agent.name`, `gen_ai.tool.name`.
- **Galaxy pipeline attributes** (on the `pipeline.run` root span emitted by `pipeline_span()` in [core/run_tracer.py](../../core/run_tracer.py)): `galaxy.run_id`, `galaxy.module`. These are the **only** `galaxy.*` keys on span dimensions; `galaxy.nhi_id`, `galaxy.agent_type`, and `galaxy.attempt` are **not** span attributes.
- **A2A attributes** ([a2a/dispatcher.py](../../a2a/dispatcher.py)): `a2a.conversation_id`, `a2a.message_id`, `a2a.sender`, `a2a.recipient`, `a2a.intent`, `a2a.payload_schema`, `a2a.status`, `a2a.latency_ms`, `a2a.request_envelope`, `a2a.response_envelope` (truncated to 8 KB each).
- **Governance audit attributes** (emitted by `OtelAuditBackend` as *span events* named `governance.<event_type>`, not span attributes — [governance/adapters/otel_audit_backend.py](../../governance/adapters/otel_audit_backend.py)): `governance.agent_id` (NHI principal), `governance.event_type`, `governance.action`, `governance.decision`, `governance.reason`, `governance.latency_ms`, plus arbitrary scalar metadata as `governance.metadata.<key>`.

**NHI attribution** is available only via `governance.agent_id` in governance audit events. It is not present on the OTel span dimensions directly.

### 7.1 Example KQL queries (Log Analytics)

```kusto
// Full run trace — all spans for one run, in order
AppTraces
| where Properties.["galaxy.run_id"] == "run-xxx"
| project TimeGenerated, Message, Properties
| order by TimeGenerated asc
```

```kusto
// Token cost by agent — sum GenAI usage per agent name
AppDependencies
| where isnotempty(Properties.["gen_ai.agent.name"])
| extend agent = tostring(Properties.["gen_ai.agent.name"]),
         in_tok = toint(Properties.["gen_ai.usage.input_tokens"]),
         out_tok = toint(Properties.["gen_ai.usage.output_tokens"])
| summarize input=sum(in_tok), output=sum(out_tok) by agent
```

```kusto
// Blocked governance decisions in the last hour
AppEvents
| where TimeGenerated > ago(1h)
| where Name startswith "governance."
| where Properties.["governance.decision"] in ("deny", "block")
| project TimeGenerated, agent=Properties.["governance.agent_id"],
          action=Properties.["governance.action"], reason=Properties.["governance.reason"]
```

The ledger can also be queried directly with SQL against `trace_ledger` (see [ledger_schema.sql](../../cloud_adapters/azure/infra/ledger_schema.sql)): full run trace by `run_id`, token cost by `agent_type`, and `outcome = 'blocked'` for denials.

---

## 8. Where each piece is configured (one-liner index)

| Concern | Configured in | Read by |
|---|---|---|
| Per-agent runtime tunables | [payload_agents/config/*.yaml](../../payload_agents/config/) | [payload_agents/config.py](../../payload_agents/config.py) |
| Runtime governance rules | [galaxy_gov/policies/*.yaml](../../galaxy_gov/policies/) | `create_governance_middleware` via the MAF stack ([cloud_adapters/azure/maf/middleware.py](../../cloud_adapters/azure/maf/middleware.py)) |
| Pre-middleware guard configs | [galaxy_gov/configs/*.yaml](../../galaxy_gov/configs/) | the prompt-injection / egress guards |
| NHI registry | [core/nhi_registry.py](../../core/nhi_registry.py) | `NHIRegistry.get(agent_type)` |
| LLM deployment + APIM key + egress | `.env` (local) / Key Vault (deployed) | [cloud_adapters/azure/gateway.py](../../cloud_adapters/azure/gateway.py), [cloud_adapters/azure/secrets.py](../../cloud_adapters/azure/secrets.py) |
| OTel exporter routing | `.env` `APPLICATIONINSIGHTS_CONNECTION_STRING` | [cloud_adapters/azure/tracing.py](../../cloud_adapters/azure/tracing.py), [core/run_tracer.py](../../core/run_tracer.py) |
| PostgreSQL ledger backend | `POSTGRES_DSN` | [cloud_adapters/azure/audit.py](../../cloud_adapters/azure/audit.py) |
| Azure infra — Method 1 (APIM, Function, Key Vault, PG, App Insights) | [cloud_adapters/azure/infra/main.bicep](../../cloud_adapters/azure/infra/main.bicep) | `az deployment group create` |
| Azure infra — Method 2 (per-persona Container Apps Jobs) | [cloud_adapters/azure/infra/aca_jobs.bicep](../../cloud_adapters/azure/infra/aca_jobs.bicep) | `az deployment group create` + [cloud_adapters/azure/orchestrator.py](../../cloud_adapters/azure/orchestrator.py) |
| Python deps | [pyproject.toml](../../pyproject.toml) `.[azure]` extra | pip / uv |
| Test fixtures | [tests/](../../tests/) | `pytest` |

---

## 9. Common debug shortcuts

```bash
# 1. Run the offline governance demo — no cloud / DB / LLM
uv run python scripts/demo_governance.py

# 2. Run the test suite (no cloud credentials needed)
uv run python -m pytest tests/ -q

# 3. Check what's currently in the venv
uv pip list --python .venv/bin/python | grep -iE "agent|opentel|azure|asyncpg|langchain|pydantic"

# 4. Run the full demo matrix (47 controls / 84 checks) against real Azure OpenAI (default cloud)
.venv/bin/python scripts/demo_agents.py --azure --extended

# 5. Run the matrix deterministically offline (CI)
.venv/bin/python scripts/demo_agents.py --fake --extended

# 6. Provision the reference APIM-proxy infrastructure (Method 1)
az deployment group create -g <rg> -f cloud_adapters/azure/infra/main.bicep \
  -p aoaiKey=<key> pgAdminPassword=<pw>

# 7. Create the ledger table on the PostgreSQL Flexible Server
psql "$POSTGRES_DSN" -f cloud_adapters/azure/infra/ledger_schema.sql

# 8. Deploy the per-persona Container Apps Jobs (Method 2 fan-out)
az deployment group create -g <rg> -f cloud_adapters/azure/infra/aca_jobs.bicep \
  -p acrPassword=<acr-pw> storageAccountKey=<sa-key>

# 9. Query governance blocks in Log Analytics (KQL)
az monitor log-analytics query -w <workspace-id> --analytics-query \
  "AppEvents | where Name startswith 'governance.' | where Properties.['governance.decision'] in ('deny','block') | limit 20"
```
