# Galaxy SDLC — Platform Status

> Last updated: 2026-05-22
> Update this file whenever a piece of infrastructure is provisioned or a feature ships.

---

## Azure Infrastructure

| Resource | Name | Status | Notes |
|---|---|---|---|
| Resource Group | `galaxyscanner-rg` | ✅ Live | East US · AI Labs subscription |
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
| Migration (ACA) | `scripts/run_pipeline_aca.py` | 🔶 Untested | Infrastructure deployed · needs `.env` on Azure Files share + first trigger |
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
| Classifier | `galaxy-classifier-mi` | `c4be541a-a1f2-433c-8166-9ebcf2d87b78` | ✅ |
| Scanner | `galaxyscanner-mi` | `e581d9ea-c4ca-411f-9946-2e784d9c4046` | ✅ |
| ASTAnalyzer | `galaxy-astanalyzer-mi` | `7d22106a-5fe0-467c-98f4-1080d8bcea4d` | ✅ |
| Analyzer | `galaxy-analyzer-mi` | `8cdc89ee-932e-4536-a563-434af7df3c9b` | ✅ |
| LambdaAnalyzer | `galaxy-lambdaanalyzer-mi` | `17de927d-a8d7-447b-90b7-d1d649009179` | ✅ |
| Architect | `galaxy-architect-mi` | `7b2e5510-bbee-4da4-a99d-e60711fa0be7` | ✅ |
| Coder | `galaxy-coder-mi` | `f51216a1-0e67-43c9-acb8-149954e8d4e0` | ✅ |
| Reviewer | `galaxy-reviewer-mi` | `b44d54a3-d329-49aa-89cb-ea35522768ba` | ✅ |
| Security | `galaxy-security-mi` | `72f1b573-1796-474e-b961-390ae8ad33fe` | ✅ |
| SecurityReviewer | `galaxy-securityreviewer-mi` | `ae944f1a-1032-4cbb-ba53-8cb73a790043` | ✅ |
| Tester | `galaxy-tester-mi` | `7eeb7e1a-b6f2-45d5-b721-2fa0b49da988` | ✅ |
| IaCGen | `galaxy-iacgen-mi` | `72728f28-0955-4378-8782-cde5fdc6dff3` | ✅ |
| SLOWatcher | `galaxy-slowatcher-mi` | `92f68691-ea09-4249-b9a1-221a5888c361` | ✅ |
| DiscoveryScanner | `galaxy-discoveryscanner-mi` | `40d042bb-a23e-4158-92f8-70accc3023c7` | ✅ |
| DiscoveryGrapher | `galaxy-discoverygrapher-mi` | `5a603c38-d178-4da4-94dd-85cedc9cd983` | ✅ |
| DiscoveryBRD | `galaxy-discoverybrd-mi` | `333b400b-170a-4ed0-9fae-42866a93b84f` | ✅ |
| DiscoveryArchitect | `galaxy-discoveryarchitect-mi` | `cc0da4ab-22fa-4707-8184-4e33c5884c3e` | ✅ |
| DiscoveryStories | `galaxy-discoverystories-mi` | `26c11983-dad1-480e-bff8-09eb8f3ad7f0` | ✅ |

> Entra audit logs per agent only appear once a job actually runs and the MI token is exercised.

---

## Pending / Next Steps

### High priority — unblocks cloud validation

- [ ] **First ACA end-to-end run**
  1. `az keyvault secret show --vault-name galaxyscanner-kv-d63cdd --name appinsights-connection-string --query value -o tsv` → paste into `.env`
  2. `az storage file upload --account-name galaxyscannersa --share-name galaxy-runs --source .env --path .env`
  3. `python scripts/run_pipeline_aca.py --source-dir legacy/aws_legacy --run-id run-$(date +%Y%m%d-%H%M%S) --module-id aws_legacy`

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
| Subscription | `8aee075f-c478-4da6-872c-ebcfef7a11c6` (AI Labs) |
| Tenant | `0d85160c-5899-44ca-acc8-db1501b993b6` |
| Resource group | `galaxyscanner-rg` · East US |
| ACR | `galaxyscannercrd63cdd.azurecr.io` |
| APIM gateway | `https://galaxyscanner-apim.azure-api.net/openai` |
| KV URL | `https://galaxyscanner-kv-d63cdd.vault.azure.net/` |
| App Insights ingestion | `https://eastus-8.in.applicationinsights.azure.com/` |
| Azure Files | `galaxyscannersa` / share: `galaxy-runs` |
