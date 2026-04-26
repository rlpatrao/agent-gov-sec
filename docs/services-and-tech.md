# Services & technology inventory

**Last updated:** 2026-04-26 (commit `cbcbf6c`)
**Scope:** Every Azure resource, external service, Python library, policy file, and env var the project depends on — with what it does, where it's configured, and current status.

For visual flow + sequence diagrams, see [architecture.md](architecture.md).
For the original phased migration plan, see [GOVERNANCE_MIGRATION_PLAN.md](../GOVERNANCE_MIGRATION_PLAN.md).

---

## 1. Azure resources

All in subscription **AI Labs (`8aee075f-c478-4da6-872c-ebcfef7a11c6`)**, tenant **Virtusa (`0d85160c-5899-44ca-acc8-db1501b993b6`)**, region **East US**, resource group **`galaxyscanner-rg`** unless noted.

| # | Resource | Name | What it does | Status | Wired in code at |
|---|---|---|---|---|---|
| 1 | Resource Group | `galaxyscanner-rg` | Container for everything below | ✅ Provisioned | — |
| 2 | Key Vault (access-policy mode) | `galaxyscanner-kv-d63cdd` | Stores `azure-openai-key`, `appinsights-connection-string`, `acr-password`. RBAC mode unavailable (your account lacks `roleAssignments/write`); access-policy mode works for Contributor. | ✅ Provisioned | [token_provider.py:50-69](../token_provider.py#L50-L69) reads `AZURE_KEY_VAULT_URL` + `DefaultAzureCredential` |
| 3 | Azure Container Registry (Basic) | `galaxyscannercrd63cdd` | Hosts `galaxy-scanner:0.2.1`, `galaxy-scanner:latest`, plus the imported `devcontainers-python:3.13` base. Admin enabled (Basic doesn't support scope-map tokens). | ✅ Provisioned | [Dockerfile:4](../Dockerfile#L4) `FROM galaxyscannercrd63cdd.azurecr.io/devcontainers-python:3.13` |
| 4 | User-Assigned Managed Identity | `galaxyscanner-mi` | The Scanner's NHI. Federated tokens via Workload Identity exchange to this MI; `clientId=e581d9ea-…` ends up in `NHI_CLIENT_ID_SCANNER`. | ✅ Provisioned | [nhi_identity.py:39-48](../nhi_identity.py#L39-L48) reads it from env |
| 5 | Log Analytics workspace | `galaxyscanner-law` | Backing store for App Insights *and* Container Apps console-log capture. customerId `56bf830a-…`. | ✅ Provisioned | linked to App Insights below |
| 6 | Application Insights (workspace-based) | `galaxyscanner-ai` | OTel span sink. Connection string in KV. Reads to KQL via the `Logs` blade or "Agents (preview)" dashboard. | ✅ Provisioned | [run_tracer.py:78-86](../run_tracer.py#L78-L86) wires `AzureMonitorTraceExporter` when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set |
| 7 | Azure OpenAI Service | `galaxyscanner-openai` | Hosts the **`gpt-5-3-codex`** deployment. Endpoint `https://galaxyscanner-openai.openai.azure.com/`. Uses the **Responses API** (`/openai/v1/responses?api-version=preview`) since codex models don't support Chat Completions. | ✅ Provisioned + deployed | [agents/scanner_agent.py:227-232](../agents/scanner_agent.py#L227-L232), [agents/ast_agent.py:192-197](../agents/ast_agent.py#L192-L197) — `OpenAIChatClient(azure_endpoint=…)` |
| 8 | Container Apps Environment | `galaxyscanner-aca-env` | The control plane for the Container App Job. Linked to `galaxyscanner-law` for stdout capture. defaultDomain `happydesert-80e42b87.eastus.azurecontainerapps.io`. | ✅ Provisioned | — (only used when Job lands) |
| 9 | Container App Job | `galaxyscanner-job` | Manual-triggered batch executing one scan run. | 🔴 **BLOCKED** — `az containerapp job create` returns InternalServerError when private-registry creds attached. Public-image jobs work; private-registry path doesn't. | — |
| 10 | Postgres Flexible Server (B1ms) | `galaxyscanner-pg` (planned) | Persistent hash-chained `trace_ledger` archive — survives container restarts, queryable by Compliance Auditor agent. | 🔶 **Deferred** | [infra/ledger_schema.sql](../infra/ledger_schema.sql) DDL ready; [governance/adapters/postgres_audit_backend.py](../governance/adapters/postgres_audit_backend.py) wired |
| 11 | API Management (Consumption) | `galaxyscanner-apim` (planned) | Reverse proxy in front of Azure OpenAI; injects `x-agent-type`, `x-galaxy-run-id`, etc. headers + per-agent rate limits + JWT auth. | 🔶 **Deferred** | not wired |
| 12 | Microsoft Foundry resource (pre-existing) | `ailab-solution-agentic-sdlc` | User's pre-existing Foundry resource in `aifoundry_rg`. Currently **unused** — Anthropic models aren't in East US for this tenant; we use Azure OpenAI instead. | ⏸ Idle | not wired |

**Live IDs and reference values:** [azure-resources.md](../azure-resources.md).

---

## 2. External services

| Service | Used today? | Notes |
|---|---|---|
| **Azure OpenAI** (`gpt-5-3-codex`) | ✅ Every LLM call | Both Scanner and ASTAnalyzer call this. Responses API, `api_version=preview`. |
| **Microsoft Container Registry** (`mcr.microsoft.com`) | ✅ Once, at image-import time | Source of `devcontainers-python:3.13` base, imported into our ACR via `az acr import` because Docker Hub is blocked by corporate proxy. |
| **Docker Hub** (`registry-1.docker.io`) | ❌ Blocked by corporate proxy | Anonymous CDN paths return 403. We never pull from here directly. |
| **Anthropic API** (`api.anthropic.com`) | ❌ Removed in Phase D | The original `foundry_client.py` called this; gone since commit `bd9d502`. The package is no longer in `requirements.txt`. |
| **Microsoft Purview** | ❌ Provider not registered | Considered for `PurviewPolicyMiddleware`; we use the YAML/`agent_os` path instead. ~$300/mo minimum if ever wanted. |

---

## 3. Python runtime stack ([requirements.txt](../requirements.txt))

### 3.1 Microsoft Agent Framework (the LLM-orchestration spine)

| Package | Version | Role |
|---|---|---|
| `agent-framework-core` | `>=1.2.0,<2` | Core `Agent`, `AgentMiddleware`, `ChatMiddleware`, observability layers, GenAI semantic conventions. We pin **core directly** (not the `agent-framework` meta) because the meta pulls `agent-framework-azure-ai-search` which clobbers the real `__init__.py` with an empty file. |
| `agent-framework-foundry` | latest | The Microsoft Foundry chat client — installed for completeness even though we use the OpenAI variant today. |
| `agent_framework_openai` (transitive) | bundled w/ core | Provides `OpenAIChatClient` (which speaks Azure OpenAI Responses API natively when `azure_endpoint=…` is passed). |

### 3.2 Microsoft Agent Governance Toolkit (policies + audit + circuit breaker)

| Package | Version | Role |
|---|---|---|
| `agent-os-kernel` | `>=3.2.2` | The runtime governance engine. Provides `agent_os.policies.PolicyEvaluator`, `agent_os.audit_logger.GovernanceAuditLogger`, `agent_os.circuit_breaker.CircuitBreaker`, `agent_os.prompt_injection.PromptInjectionDetector`, and `agent_os.integrations.maf_adapter` (the MAF middleware the project uses). |
| `agent-sre` | `==3.2.2` (exact) | Pinned exact because `agent_os.integrations.maf_adapter` imports `agent_sre.anomaly.RogueAgentDetector`, which only exists in 3.2.2 (renamed in 1.1.2). |
| `agentmesh-platform` | `>=3.2.2` | Required transitively by `agent_os.integrations.maf_adapter` (`from agentmesh.governance import AuditEntry, AuditLog`). Without it, container imports fail. |

### 3.3 Azure SDK

| Package | Version | Role |
|---|---|---|
| `azure-identity` | `>=1.19.0` | `DefaultAzureCredential` for Workload Identity → AAD token. |
| `azure-keyvault-secrets` | `>=4.8.0` | `SecretClient` for fetching `azure-openai-key`. |
| `azure-monitor-opentelemetry-exporter` | `>=1.0.0b50` | OTel `SpanExporter` that POSTs to App Insights' `v2.1/track` endpoint. Beta is the only version on PyPI today. |

### 3.4 OpenTelemetry

| Package | Version | Role |
|---|---|---|
| `opentelemetry-api` | `>=1.27.0` | `trace.get_tracer`, span context, propagation. |
| `opentelemetry-sdk` | `>=1.27.0` | `TracerProvider`, `BatchSpanProcessor`, `Resource`. |
| `opentelemetry-exporter-otlp-proto-grpc` | `>=1.27.0` | Fallback exporter for OTLP collectors (unused today; AzureMonitorExporter wins when AI connection string is set). |
| `opentelemetry-instrumentation-fastapi` | `>=0.48b0` | Auto-instrumentation for the future human-gate FastAPI endpoint. |

### 3.5 Tree-sitter (deterministic AST parser)

| Package | Version | Role |
|---|---|---|
| `tree-sitter` | `>=0.23` | Core C-extension parser bindings. |
| `tree-sitter-python` | `>=0.23` | Python grammar (verified working: parses `def hello(): return 42` to `module(1 child)`). |
| `tree-sitter-java` | `>=0.23` | Java grammar (Spring + JPA support verified in [tests/test_ast_extractor.py](../tests/test_ast_extractor.py)). |

### 3.6 Storage / config / web

| Package | Version | Role |
|---|---|---|
| `asyncpg` | `>=0.29.0` | Async Postgres driver — used by `PostgresHashChainBackend` once `POSTGRES_DSN` is set. |
| `pydantic` | `>=2.0.0,<3` | Schema validation for `agents/config/*.yaml`. Already transitive via `agent-framework-core`; pinned explicitly so MAF version bumps can't drag us across major Pydantic lines. |
| `PyYAML` | `>=6.0.1` | Reads YAML config + governance policies. |
| `python-dotenv` | `>=1.0.0` | Loads `.env` for local dev. |
| `fastapi` + `uvicorn` | `>=0.115` / `>=0.32` | Future human-gate endpoint. Not currently mounted. |

### 3.7 Test

| Package | Version | Role |
|---|---|---|
| `pytest` | `>=8.0.0` | Test runner. |
| `pytest-asyncio` | `>=0.24.0` | `@pytest.mark.asyncio` for the async governance tests. |

### 3.8 Removed (intentionally)

Documented at [requirements.txt:50-54](../requirements.txt#L50-L54). All replaced by something MAF or `agent_os` ships:

| Removed | Replaced by |
|---|---|
| `tenacity` | `agent_os.circuit_breaker.CircuitBreaker` |
| `anthropic` | `agent_framework_anthropic` if ever needed; not used today |
| `openai` (raw SDK) | `agent_framework_openai.OpenAIChatClient` (still wraps `openai`, but as a transitive dep with the right version pin) |
| `claude-agent-sdk` | Plain `os.walk` traversal in [agents/scanner_agent.py:134-158](../agents/scanner_agent.py#L134-L158); MAF for the LLM call |

---

## 4. Local tooling

| Tool | Why it's used | Notes |
|---|---|---|
| `python` 3.13 / 3.14 | Runtime | Both venvs work; `tree-sitter` requires C-ext rebuild per minor Python version. |
| `uv` | Faster pip resolver / venv manager | Used for `uv pip install` in venv operations. |
| `az` (Azure CLI) | All Azure provisioning + queries | Subject to corporate-proxy CA caveats — see [docs/toolkit-verification.md](toolkit-verification.md). |
| `docker` (Docker Desktop) | Build the container image | Cannot pull from Docker Hub on corporate network; uses ACR-imported base images. |
| `git` | Source control | Single branch (`main`) so far — `pre-maf-port` tag was on the old repo location. |
| `gh` (GitHub CLI) | Not yet used | No GitHub remote configured on this repo. |

---

## 5. Governance policies (YAML on disk)

All loaded by `PolicyEvaluator.load_policies(governance/policies/)` at [governance/middleware.py:78-94](../governance/middleware.py#L78-L94).

| File | What it enforces |
|---|---|
| [governance/policies/galaxy-core.yaml](../governance/policies/galaxy-core.yaml) | Prompt-injection regex (OWASP ASI-01), oversized-prompt gate (~24 KB ≈ 6 K tokens) |
| [governance/policies/galaxy-tools.yaml](../governance/policies/galaxy-tools.yaml) | Per-agent tool allow-list (Scanner: `read_file`, `list_directory`, `stat_file`; deny network egress) |
| [governance/policies/galaxy-pii.yaml](../governance/policies/galaxy-pii.yaml) | Placeholder for PII rules — currently `defaults.action=allow` (no-op) until Presidio/Content Safety is wired |
| [governance/policies/galaxy-ast.yaml](../governance/policies/galaxy-ast.yaml) | AST-agent-specific rules (deny outbound A2A from leaf agent, etc.) |

### Per-agent config (separate from policies)

| File | What it tunes |
|---|---|
| [agents/config/scanner.yaml](../agents/config/scanner.yaml) | `max_file_scan_bytes=50000`, `allowed_recipients=[ASTAnalyzer]`, `max_files_per_dispatch=40`, `timeout_seconds=30` |
| [agents/config/ast_analyzer.yaml](../agents/config/ast_analyzer.yaml) | `max_file_scan_bytes=256000`, leaf-node (`allowed_recipients=[]`), `timeout_seconds=60` |

---

## 6. Environment variables (the `.env` and ACA contracts)

| Variable | Purpose | Set today (local)? | Read at |
|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | Foundry endpoint URL | ✅ | [agents/scanner_agent.py:225](../agents/scanner_agent.py#L225) |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (`gpt-5-3-codex`) | ✅ | same |
| `AZURE_OPENAI_API_VERSION` | `preview` (literal) for Responses API | ✅ | same |
| `AZURE_OPENAI_KEY` | Local-only; ACA fetches from KV instead | ✅ | [token_provider.py:96](../token_provider.py#L96) fallback path |
| `AZURE_KEY_VAULT_URL` | KV URL; **blank locally** so env-var fallback wins | ✅ (blank) | [token_provider.py:50](../token_provider.py#L50) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights connection | ✅ | [run_tracer.py:77](../run_tracer.py#L77) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector fallback | ❌ unset (App Insights wins) | [run_tracer.py:78](../run_tracer.py#L78) |
| `OTEL_SERVICE_NAME` | OTel resource attribute | ✅ `galaxy-scanner-local` | [run_tracer.py:74](../run_tracer.py#L74) |
| `POSTGRES_DSN` | Async postgres connection | ❌ blank (stdout mode) | [governance/adapters/postgres_audit_backend.py:46-50](../governance/adapters/postgres_audit_backend.py#L46-L50) |
| `NHI_CLIENT_ID_SCANNER` | Entra MI clientId for Scanner | ✅ placeholder `local-scanner-nhi` | [nhi_identity.py:39](../nhi_identity.py#L39) |
| `NHI_CLIENT_ID_ARCHITECT` | (future agent) | ✅ placeholder | same |
| `NHI_CLIENT_ID_CODER` | (future agent) | ✅ placeholder | same |
| `NHI_CLIENT_ID_REVIEWER` | (future agent) | ✅ placeholder | same |
| `NHI_CLIENT_ID_SECURITY` | (future agent) | ✅ placeholder | same |
| `AZURE_CLIENT_ID` | Hint for `DefaultAzureCredential` to pick a specific MI when multiple are attached | only set inside ACA Job spec | — |
| `CLAUDE_CODE_USE_FOUNDRY` | Claude Code CLI setting (unrelated to scanner) | ✅ `1` | not read by scanner |

For a copy-pasteable example see [.env.example](../.env.example).

---

## 7. Telemetry attribute vocabulary

The keys you can `customDimensions[…]` against in App Insights KQL. Sources:

- **GenAI semantic conventions** (emitted by MAF) — full list in [agent_framework.observability.OtelAttr](../.venv/lib/python3.14/site-packages/agent_framework/observability.py#L170): `gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.agent.name`, `gen_ai.tool.name`, etc.
- **Galaxy attributes** (emitted by our `RunTracer.agent_span`) at [run_tracer.py:160-168](../run_tracer.py#L160-L168): `galaxy.run_id`, `galaxy.module_id`, `galaxy.agent_type`, `galaxy.attempt`, `galaxy.nhi_id`.
- **A2A attributes** at [a2a/dispatcher.py:105-114](../a2a/dispatcher.py#L105-L114) and [a2a/dispatcher.py:140-149](../a2a/dispatcher.py#L140-L149): `a2a.conversation_id`, `a2a.message_id`, `a2a.sender`, `a2a.recipient`, `a2a.intent`, `a2a.payload_schema`, `a2a.status`, `a2a.latency_ms`, `a2a.request_envelope`, `a2a.response_envelope` (truncated to 8 KB each).
- **Governance audit attributes** (emitted by `OtelAuditBackend`) at [governance/adapters/otel_audit_backend.py:42-71](../governance/adapters/otel_audit_backend.py#L42-L71): `governance.agent_id`, `governance.event_type`, `governance.action`, `governance.decision`, `governance.reason`, `governance.latency_ms`, plus arbitrary scalar metadata flattened as `governance.metadata.<key>`.

---

## 8. Where each piece is configured (one-liner index)

| Concern | Configured in | Read by |
|---|---|---|
| Azure resource IDs + connection strings | [azure-resources.md](../azure-resources.md) | (humans only) |
| Per-agent runtime tunables | [agents/config/*.yaml](../agents/config/) | [agents/config.py](../agents/config.py) |
| Runtime governance rules | [governance/policies/*.yaml](../governance/policies/) | [governance/middleware.py](../governance/middleware.py) → `agent_os.policies.PolicyEvaluator` |
| LLM endpoint + model + key | [.env](../.env) (local) / Key Vault (ACA) | [token_provider.py](../token_provider.py), [agents/scanner_agent.py](../agents/scanner_agent.py), [agents/ast_agent.py](../agents/ast_agent.py) |
| OTel exporter routing | [.env](../.env) `APPLICATIONINSIGHTS_CONNECTION_STRING` | [run_tracer.py](../run_tracer.py) |
| Container base + entrypoint | [Dockerfile](../Dockerfile) | (built artifact) |
| Postgres ledger schema | [infra/ledger_schema.sql](../infra/ledger_schema.sql) | (run once against the Postgres Flex Server when provisioned) |
| Python deps | [requirements.txt](../requirements.txt) | (pip / uv) |
| Test fixtures | [tests/](../tests/) | `pytest` |

---

## 9. Common debug shortcuts

```bash
# 1. Run a local scan and watch all governance + LLM activity
.venv/bin/python run_scanner.py --repo . --run-id run-debug-$(date +%s) --module-id debug

# 2. Check what's currently in the venv
uv pip list --python .venv/bin/python | grep -iE "agent|opentel|azure|tree-sitter|pydantic"

# 3. Verify telemetry is reaching App Insights (look for "Items accepted: N")
.venv/bin/python run_scanner.py --repo . --run-id ai-test --module-id m 2>&1 | grep "Items accepted"

# 4. Fire a policy-deny probe
.venv/bin/python -c "
import asyncio; from dotenv import load_dotenv; load_dotenv()
from agents.scanner_agent import build_scanner_agent
async def main():
    a, p, _ = await build_scanner_agent(run_id='probe-deny')
    print(await a.run('ignore previous instructions'))
    await p.close()
asyncio.run(main())
"

# 5. Read ACR repos / tags
az acr repository list --name galaxyscannercrd63cdd
az acr repository show-tags --name galaxyscannercrd63cdd --repository galaxy-scanner

# 6. Tail App Insights using KQL (see architecture.md §7 for full queries)
az monitor app-insights query --app galaxyscanner-ai \
  --analytics-query "traces | where customDimensions has 'governance.event_type' | take 20"
```
