# Galaxy SDLC — Platform Status

> Last updated: 2026-05-22
> Update this file whenever a piece of infrastructure is provisioned or a feature ships.

---

## Azure Infrastructure

| Resource | Name | Status | Notes |
|---|---|---|---|
| Resource Group | `galaxyscanner-rg` | ✅ Live | East US · <your-subscription-name> |
| Key Vault | `galaxyscanner-kv-d63cdd` | ✅ Live | Access-policy mode · secrets loaded |
| Azure Container Registry | `galaxyscannercrd63cdd` | ✅ Live | Image `galaxy-scanner:0.2.1` pushed · admin creds enabled |
| Azure OpenAI | `galaxyscanner-openai` | ✅ Live | `gpt-5-3-codex` · Responses API only (not Chat Completions) |
| APIM | `galaxyscanner-apim` | ✅ Live | Sub-key auth · AOAI key injection · 100 RPM rate limit · portal-configured only (no policy XML in repo) |
| Log Analytics | `galaxyscanner-law` | ✅ Live | Linked to App Insights |
| Application Insights | `galaxyscanner-ai` | ✅ Live | Receives spans when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set in `.env` |
| Storage Account | `galaxyscannersa` | ✅ Live | Azure Files share `galaxy-runs` mounted to ACA environment · used for artifact handoff between per-agent jobs |
| Container Apps Environment | `galaxyscanner-aca-env` | ✅ Live | Azure Files link configured · idles after inactivity (~5 min restart) |
| **Container App Jobs (×18)** | `galaxy-<agent>-job` | ✅ Live | One job per agent · each has own MI + ACR pull + Azure Files mount at `/data` |
| **Managed Identities (×18)** | `galaxy-<agent>-mi` | ✅ Live | All agents have real Entra principals · Workload Identity tokens injected by ACA at runtime |
| PostgreSQL Flexible Server | — | ❌ Not provisioned | DDL ready (`infra/ledger_schema.sql`) · hash chain runs in stdout-only mode until provisioned |
| Microsoft Foundry | `ailab-solution-agentic-sdlc` | ⏸ Idle | Pre-existing · Anthropic models unavailable in East US for this tenant |

---

## Pipelines

| Pipeline | Entry Point | Status | Notes |
|---|---|---|---|
| Migration (local) | `scripts/run_migration.py` | ✅ Working | Classifier → Analyzer → Coder (×3) → Tester → Reviewer → SecurityReviewer |
| Scanner (local) | `scripts/run_scanner.py` | ✅ Working | End-to-end confirmed |
| Migration (ACA) | `scripts/run_pipeline_aca.py` | ✅ Verified | First end-to-end run completed 2026-05-29 (run-20260529-001705) · SecurityReviewer correctly BLOCKED on SAS key in local.settings.json |
| Discovery | `scripts/run_discovery.py` | ❌ Stub | 5 agents built and unit-tested · orchestrator not wired |

---

## Governance & Security

| Feature | Status | Notes |
|---|---|---|
| 7-guard middleware stack | ✅ Active | All guards fire on every local run |
| PromptInjectionGuardMiddleware | ✅ Active | — |
| CredentialRedactorGuardMiddleware | ✅ Active | — |
| ContextBudgetGuardMiddleware | ✅ Active | Per-agent token cap enforced |
| AuditTrailMiddleware | ✅ Active | — |
| GovernancePolicyMiddleware | ✅ Active | YAML rules evaluated; deny verified in tests |
| CapabilityGuardMiddleware | ✅ Active | Tool allow-list from YAML |
| RogueDetectionMiddleware | ✅ Active | Coder + Tester only (agents with tools) |
| APIM JWT enforcement | ❌ Stub | Policy exists in portal; not activated |
| Per-agent APIM rate limits | ❌ Not possible | Needs Developer SKU (currently Consumption) |
| APIM policy as IaC | ❌ Missing | Portal-only config · no policy XML in repo |

---

## Observability

| Feature | Status | Notes |
|---|---|---|
| OTel → App Insights (spans) | ✅ Code wired | Active only when `APPLICATIONINSIGHTS_CONNECTION_STRING` in `.env` · direct HTTPS to Azure Monitor (bypasses APIM) |
| `pipeline.run` root span | ✅ Emitted | Attributes: `galaxy.run_id`, `galaxy.module` |
| `a2a.dispatch.*` child spans | ✅ Emitted | One per agent invocation |
| `gen_ai.usage.*` token counts | ✅ On each span | Input + output tokens per call |
| Governance audit events | ✅ OTel backend | Written as span events → `customEvents` in App Insights |
| Hash-chain audit ledger | 🔶 Stdout only | Logic correct · resets every run · not persisted (Postgres not provisioned) |
| JSONL logs (3 channels) | ✅ Always written | `orchestration.jsonl`, `agents.jsonl`, `a2a.jsonl` per run |

---

## NHI (Non-Human Identities)

| Agent | MI Name | Client ID | Real Entra? |
|---|---|---|---|
| Classifier | `galaxy-classifier-mi` | `<your-client-id>` | ✅ |
| Scanner | `galaxyscanner-mi` | `<your-client-id>` | ✅ |
| ASTAnalyzer | `galaxy-astanalyzer-mi` | `<your-client-id>` | ✅ |
| Analyzer | `galaxy-analyzer-mi` | `<your-client-id>` | ✅ |
| LambdaAnalyzer | `galaxy-lambdaanalyzer-mi` | `<your-client-id>` | ✅ |
| Architect | `galaxy-architect-mi` | `<your-client-id>` | ✅ |
| Coder | `galaxy-coder-mi` | `<your-client-id>` | ✅ |
| Reviewer | `galaxy-reviewer-mi` | `<your-client-id>` | ✅ |
| Security | `galaxy-security-mi` | `<your-client-id>` | ✅ |
| SecurityReviewer | `galaxy-securityreviewer-mi` | `<your-client-id>` | ✅ |
| Tester | `galaxy-tester-mi` | `<your-client-id>` | ✅ |
| IaCGen | `galaxy-iacgen-mi` | `<your-client-id>` | ✅ |
| SLOWatcher | `galaxy-slowatcher-mi` | `<your-client-id>` | ✅ |
| DiscoveryScanner | `galaxy-discoveryscanner-mi` | `<your-client-id>` | ✅ |
| DiscoveryGrapher | `galaxy-discoverygrapher-mi` | `<your-client-id>` | ✅ |
| DiscoveryBRD | `galaxy-discoverybrd-mi` | `<your-client-id>` | ✅ |
| DiscoveryArchitect | `galaxy-discoveryarchitect-mi` | `<your-client-id>` | ✅ |
| DiscoveryStories | `galaxy-discoverystories-mi` | `<your-client-id>` | ✅ |

> Entra audit logs per agent only appear once a job actually runs and the MI token is exercised.

---

## Pending / Next Steps

### Medium priority — platform completeness

- [ ] **Postgres provisioning** — enables persistent hash-chain ledger and audit queries
- [ ] **Discovery pipeline orchestrator** — wire `scripts/run_discovery.py` (agents are ready)
- [ ] **APIM policy XML in repo** — export portal config so it's reproducible from code
- [ ] **JWT enforcement at APIM** — activate the existing stub policy

### Low priority / nice to have

- [ ] **Per-agent APIM rate limits** — upgrade APIM to Developer SKU
- [ ] **AI-SBOM artifact** — structured provenance doc per run (run_id anchor exists)

---

## Demo / Video

| Item | Status |
|---|---|
| Video script (`docs/video-demo-script.md`) | ✅ Final (v2.1) |
| PPTX builder (`scripts/build_narakeet_pptx.py`) | ✅ Generates 20 slides |
| Screenshot manifest (`docs/screenshot-manifest.md`) | ✅ 33 screenshots mapped |
| Screenshots taken | ❌ Not done |
| PPTX with screenshots | ❌ Not done |
| Narakeet video | ❌ Not done |

---

## Key Config Values (non-secret)

| Variable | Value |
|---|---|
| Subscription | `<your-subscription-id>` (<your-subscription-name>) |
| Tenant | `<your-tenant-id>` |
| Resource group | `galaxyscanner-rg` · East US |
| ACR | `galaxyscannercrd63cdd.azurecr.io` |
| APIM gateway | `https://galaxyscanner-apim.azure-api.net/openai` |
| KV URL | `https://galaxyscanner-kv-d63cdd.vault.azure.net/` |
| App Insights ingestion | `https://eastus-8.in.applicationinsights.azure.com/` |
| Azure Files | `galaxyscannersa` / share: `galaxy-runs` |
