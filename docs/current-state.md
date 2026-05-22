# Galaxy SDLC — Current State (as of 2026-05-21)

A factual, code-verified snapshot. No aspirational claims — only what has been confirmed to run. For design intent see `architecture.md`; for the roadmap see `GOVERNANCE_MIGRATION_PLAN.md`.

---

## 1. Where the platform actually runs

**Everything runs locally today.** The pipeline is invoked from a developer laptop:

```bash
uv run python scripts/run_migration.py --source-dir legacy/aws_legacy
uv run python scripts/run_scanner.py --repo legacy/aws_legacy --run-id run-001 --module-id aws_legacy
```

The Container App Job (`galaxyscanner-job`) that would host this in Azure is **blocked** — `az containerapp job create` returns `InternalServerError` when private-registry credentials are attached. The Container Apps environment (`galaxyscanner-aca-env`) is provisioned and linked to Log Analytics, but no job has ever successfully executed in it.

---

## 2. Azure infrastructure — actual state

Resource group: **`galaxyscanner-rg`** · Subscription: **AI Labs** · Region: **East US**

| Resource | Name | Actual state | Gap / caveat |
|---|---|---|---|
| Resource Group | `galaxyscanner-rg` | ✅ Provisioned | — |
| Key Vault | `galaxyscanner-kv-d63cdd` | ✅ Provisioned, secrets loaded | Access-policy mode only — account lacks `roleAssignments/write` so RBAC mode unavailable |
| Azure Container Registry | `galaxyscannercrd63cdd` | ✅ Provisioned, image pushed (`galaxy-scanner:0.2.1`) | Admin creds enabled (Basic SKU, no scope-map tokens) |
| Managed Identity | `galaxyscanner-mi` | ✅ Provisioned (`clientId=e581d9ea-…`) | **Only 1 of 18 NHIs provisioned** — this is the Scanner's. All other agents use placeholder strings. |
| Log Analytics workspace | `galaxyscanner-law` | ✅ Provisioned, linked to App Insights | — |
| Application Insights | `galaxyscanner-ai` | ✅ Provisioned, receiving spans | Connection string in KV; must be set in `.env` locally |
| Azure OpenAI | `galaxyscanner-openai` | ✅ Provisioned + `gpt-5-3-codex` deployed | Uses Responses API (`/openai/v1/responses?api-version=preview`) — Chat Completions not supported for this model |
| APIM (Consumption) | `galaxyscanner-apim` | ✅ Provisioned, policy live (portal-configured) | No policy XML in repo — portal-only config. JWT stub in place, not enforced. Per-agent rate limits need Developer SKU. |
| Container Apps Environment | `galaxyscanner-aca-env` | ✅ Provisioned | Only the control plane — no job has run in it |
| Container App Job | `galaxyscanner-job` | 🔴 Blocked | `az containerapp job create` with private-registry creds → `InternalServerError`. Public-image jobs work; private ACR pull path broken in Azure API. |
| PostgreSQL Flexible Server | `galaxyscanner-pg` | 🔶 Not provisioned | DDL ready (`infra/ledger_schema.sql`), code wired — waiting on provisioning decision |
| Microsoft Foundry resource | `ailab-solution-agentic-sdlc` | ⏸ Idle | Pre-existing resource, not wired — Anthropic models not available in East US for this tenant |

---

## 3. Instrumentation — what's actually emitting data

### 3.1 OTel → Application Insights

| Component | Status | Condition |
|---|---|---|
| `configure_tracing()` wiring | ✅ Code confirmed | Runs at process start in `scripts/run_migration.py` |
| `AzureMonitorTraceExporter` | ✅ Active when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set | Connection string is in KV (`appinsights-connection-string`); must be in `.env` locally |
| `pipeline.run` root span | ✅ Emitted | Attributes: `galaxy.run_id`, `galaxy.module` only |
| `a2a.dispatch.*` child spans | ✅ Emitted by MAF `AgentTelemetryLayer` | One span per agent invocation — `a2a.dispatch.Analyzer`, `a2a.dispatch.Coder`, etc. |
| `gen_ai.usage.*` token counts | ✅ On each agent span | Input and output tokens per call |
| MAF `ChatTelemetryLayer` + `AgentTelemetryLayer` | ✅ Wired via `configure_otel_providers()` | Falls back to minimal `TracerProvider` if MAF not installed |
| **OTLP fallback** | ✅ Code path exists | Used if `OTEL_EXPORTER_OTLP_ENDPOINT` set instead of App Insights string |
| **No-exporter mode** | ✅ Safe default for tests | Neither env var set → tracing is no-op, no error |

### 3.2 Governance audit events

| Component | Status | Condition |
|---|---|---|
| `OtelAuditBackend` | ✅ Active | Writes every `AuditEntry` as a span event on the current OTel span → appears in App Insights as `customEvents` with `governance.*` dimensions |
| `PostgresHashChainBackend` | ✅ Code wired | **Inactive** — `POSTGRES_DSN` is blank; falls through to stdout mode |
| Hash-chain logic | ✅ Runs in stdout mode | SHA-256 chain computed and logged to stdout; not persisted anywhere; chain resets on every process start |
| `governance.agent_id` on audit events | ✅ Present | NHI `agent_id` string (e.g., `Coder-local-coder-nhi`) attached to every governance event |

### 3.3 Structured JSONL logs (3 channels)

| Channel | Status | Location |
|---|---|---|
| `orchestration.jsonl` | ✅ Written per run | `migrated/<repo>/vN/logs/<run_id>/` — phase start/end events |
| `agents.jsonl` | ✅ Written per run | Per-LLM-call: agent, latency_ms, tokens_in, tokens_out, cost_usd |
| `a2a.jsonl` | ✅ Written per run | Per A2A dispatch: sender, recipient, intent, latency_ms, status |

### 3.4 NHI (Non-Human Identity)

| What | Status | Reality |
|---|---|---|
| NHI registry code (`NHIRegistry`) | ✅ Working | 18 types registered in `core/nhi_identity.py` |
| `x-nhi-id` header on APIM requests | ✅ Sent | Header carries the `client_id` string on every LLM call |
| **Real Entra Managed Identity** | 🔶 Scanner only | Only `galaxyscanner-mi` (`e581d9ea-…`) is a real Entra principal. All other 17 agents send placeholder strings like `local-coder-nhi`. These are accepted by APIM (it doesn't validate the NHI value, only the subscription key) but have no Entra audit log entry. |
| `ManagedIdentityCredential` | ⏸ Not activated locally | `AgentIdentity.get_credential()` returns `None` when `azure.identity` resolves to the laptop's `DefaultAzureCredential` without a matching MI attached |

### 3.5 APIM gateway

| What | Status | Source of truth |
|---|---|---|
| Subscription key sent by agents | ✅ Code confirmed | `agents/_base.py:106` — `Ocp-Apim-Subscription-Key` header |
| Subscription key validated by APIM | ✅ Live (portal-configured) | APIM default behaviour for any API with "subscription required" product |
| Required-headers guard (`x-agent-type`, `x-galaxy-run-id`) | ✅ Live (portal-configured) | `services-and-tech.md §1` row 11 — confirmed in portal; no policy XML in repo |
| AOAI key injected from KV named value | ✅ Live (portal-configured) | Key never leaves Azure control plane — confirmed |
| 100 RPM rate-limit | ✅ Live (per-subscription, not per-agent) | Per-agent limits require Developer SKU |
| JWT enforcement | ⏸ Stub only | `validate-jwt` policy in place in portal; **not enforced** |
| APIM policy XML in repo | ❌ Not present | Portal-only configuration — no IaC for APIM policies |

---

## 4. Governance middleware — what's active per run

All 7 guards are wired and active for every agent call when running locally. Order matters — earlier guards fail fast before later ones run.

| # | Guard | Active? | Confirmed by |
|---|---|---|---|
| 1 | `PromptInjectionGuardMiddleware` | ✅ | `governance/guards/prompt_injection.py` + `tests/test_guards.py` |
| 2 | `CredentialRedactorGuardMiddleware` | ✅ | `governance/guards/credential_redactor.py` + test suite |
| 3 | `ContextBudgetGuardMiddleware` | ✅ | `governance/guards/context_budget.py` — per-agent token cap enforced |
| 4 | `AuditTrailMiddleware` | ✅ | `agent_os.integrations.maf_adapter` via `_CompatAuditLogger` shim |
| 5 | `GovernancePolicyMiddleware` | ✅ — live deny verified | YAML rules evaluated; `tests/test_guards.py` confirms deny fires |
| 6 | `CapabilityGuardMiddleware` | ✅ | Tool allow-list from YAML; deny-unknown enforced at build time |
| 7 | `RogueDetectionMiddleware` | ✅ for Coder + Tester | Active only when agent has tools. No-op for Analyzer, Reviewer, SecurityReviewer. |

---

## 5. Pipeline — what runs end-to-end

| Pipeline | Entry point | Status |
|---|---|---|
| Migration pipeline | `scripts/run_migration.py` | ✅ Runs end-to-end locally |
| Scanner pipeline | `scripts/run_scanner.py` | ✅ Runs end-to-end locally |
| Discovery pipeline (5 agents) | No orchestrator yet | 🔶 Agents built and tested individually; `scripts/run_discovery.py` is a stub |

**Migration pipeline phases (all confirmed working):**

| Phase | Agent | Confirmed |
|---|---|---|
| 0 | `RepoClassifier` (no LLM, <100ms) | ✅ |
| 1 | `Analyzer` | ✅ |
| 2 | `Coder` (×3 self-healing attempts, sandboxed write_file / apply_patch / validate_bicep) | ✅ |
| 3 | `Tester` (sandboxed pytest subprocess, 120s timeout) | ✅ |
| 4 | `Reviewer` | ✅ |
| 5 | `SecurityReviewer` (phase 1: OWASP regex; phase 2: LLM) | ✅ |
| — | BLOCKED verdict aborts pipeline with exit code 1 | ✅ |

---

## 6. What is not working / not yet real

| Item | Honest state |
|---|---|
| **Container Apps deployment** | Environment provisioned; job blocked on private-registry API bug. Platform runs locally only. |
| **17 of 18 NHIs** | Placeholder strings. Only Scanner has a real Entra MI. Migration and discovery agents have no Entra audit log entry. |
| **Persistent audit ledger** | Postgres not provisioned. Hash chain runs in stdout mode — not persisted across runs, not queryable. |
| **APIM policy as IaC** | No policy XML in repo. Portal-configured only. Can't reproduce the APIM config from the codebase. |
| **JWT enforcement at APIM** | Stub policy exists in portal; not activated. |
| **Per-agent APIM rate limits** | Not possible on Consumption tier. Would need Developer SKU upgrade. |
| **Workload Identity for agents** | Only works if the Container App Job is running with the MI attached. Not active locally. |
| **Discovery pipeline orchestrator** | `scripts/run_discovery.py` exists but is a stub — no end-to-end discovery run possible yet. |
| **App Insights data** | Only flows when `APPLICATIONINSIGHTS_CONNECTION_STRING` is in `.env`. Not set by default. |
| **Cross-process A2A** | All A2A calls are in-process function calls today. Networked A2A (actual HTTP between agents) is not implemented. |
| **AI-SBOM** | Not generated. `run_id` exists as a provenance anchor but no structured SBOM artifact is produced. |

---

*Last verified: 2026-05-21 — cross-checked against `core/`, `agents/_base.py`, `governance/middleware.py`, `docs/services-and-tech.md`, `docs/azure-resources.md`, architecture.md status snapshot.*
