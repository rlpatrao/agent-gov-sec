# Azure Resources — Galaxy SDLC Platform

> Last updated: 2026-05-22 · Verified against `az resource list --resource-group galaxyscanner-rg`
> Subscription IDs, tenant IDs, and NHI client IDs have been redacted. Replace `<your-...>` placeholders with your own deployment values.

---

## Subscription

| | |
|---|---|
| Subscription ID | `<your-subscription-id>` |
| Tenant ID | `<your-tenant-id>` |
| Resource Group | `galaxyscanner-rg` · East US |

---

## Core Platform Services

| Resource | Name | Status | Key identifiers / notes |
|---|---|---|---|
| Key Vault | `galaxyscanner-kv-d63cdd` | ✅ Live | `https://galaxyscanner-kv-d63cdd.vault.azure.net/` · access-policy mode (no RBAC — account lacks `roleAssignments/write`) |
| Azure Container Registry | `galaxyscannercrd63cdd` | ✅ Live | `galaxyscannercrd63cdd.azurecr.io` · Basic SKU · admin creds enabled · image `galaxy-scanner:0.2.1` pushed |
| Azure OpenAI | `galaxyscanner-openai` | ✅ Live | Deployment: `gpt-5-3-codex` · Responses API (`/openai/v1/responses?api-version=preview`) · Chat Completions not supported for this model |
| APIM | `galaxyscanner-apim` | ✅ Live | Consumption tier · sub-key + required-headers guard + 100 RPM + AOAI key injection · portal-configured only (no policy XML in repo) |
| Log Analytics workspace | `galaxyscanner-law` | ✅ Live | customerId: `<your-law-workspace-id>` · linked to App Insights |
| Application Insights | `galaxyscanner-ai` | ✅ Live | OTel span sink · connection string in KV (`appinsights-connection-string`) · ingestion endpoint: `https://eastus-8.in.applicationinsights.azure.com/` |
| Storage Account | `galaxyscannersa` | ✅ Live | Azure Files share `galaxy-runs` · mounted to ACA environment · artifact handoff between per-agent jobs |
| Microsoft Foundry | `ailab-solution-agentic-sdlc` | ⏸ Idle | Pre-existing resource · Anthropic models unavailable in East US for this tenant |

---

## Container Apps

| Resource | Name | Status | Notes |
|---|---|---|---|
| Environment | `galaxyscanner-aca-env` | ✅ Live | Linked to `galaxyscanner-law` · Azure Files `galaxy-runs` share mounted · idles after inactivity (~5 min restart) |
| Job — Classifier | `galaxy-classifier-job` | ✅ Deployed | MI: `galaxy-classifier-mi` |
| Job — Scanner | `galaxy-scanner-job` | ✅ Deployed | MI: `galaxyscanner-mi` |
| Job — ASTAnalyzer | `galaxy-astanalyzer-job` | ✅ Deployed | MI: `galaxy-astanalyzer-mi` |
| Job — Analyzer | `galaxy-analyzer-job` | ✅ Deployed | MI: `galaxy-analyzer-mi` |
| Job — LambdaAnalyzer | `galaxy-lambdaanalyzer-job` | ✅ Deployed | MI: `galaxy-lambdaanalyzer-mi` |
| Job — Architect | `galaxy-architect-job` | ✅ Deployed | MI: `galaxy-architect-mi` |
| Job — Coder | `galaxy-coder-job` | ✅ Deployed | MI: `galaxy-coder-mi` |
| Job — Reviewer | `galaxy-reviewer-job` | ✅ Deployed | MI: `galaxy-reviewer-mi` |
| Job — Security | `galaxy-security-job` | ✅ Deployed | MI: `galaxy-security-mi` |
| Job — SecurityReviewer | `galaxy-securityreviewer-job` | ✅ Deployed | MI: `galaxy-securityreviewer-mi` |
| Job — Tester | `galaxy-tester-job` | ✅ Deployed | MI: `galaxy-tester-mi` |
| Job — IaCGen | `galaxy-iacgen-job` | ✅ Deployed | MI: `galaxy-iacgen-mi` |
| Job — SLOWatcher | `galaxy-slowatcher-job` | ✅ Deployed | MI: `galaxy-slowatcher-mi` |
| Job — DiscoveryScanner | `galaxy-discoveryscanner-job` | ✅ Deployed | MI: `galaxy-discoveryscanner-mi` |
| Job — DiscoveryGrapher | `galaxy-discoverygrapher-job` | ✅ Deployed | MI: `galaxy-discoverygrapher-mi` |
| Job — DiscoveryBRD | `galaxy-discoverybrd-job` | ✅ Deployed | MI: `galaxy-discoverybrd-mi` |
| Job — DiscoveryArchitect | `galaxy-discoveryarchitect-job` | ✅ Deployed | MI: `galaxy-discoveryarchitect-mi` |
| Job — DiscoveryStories | `galaxy-discoverystories-job` | ✅ Deployed | MI: `galaxy-discoverystories-mi` |

All 18 jobs: trigger type `Manual` · replica timeout 3600s · Azure Files mounted at `/data` · private ACR pull configured via Bicep (`infra/aca_jobs.bicep`).

---

## Managed Identities (NHI — Non-Human Identities)

All 18 are real Entra Managed Identity principals in tenant `<your-tenant-id>`.

| Agent | MI Name | Client ID |
|---|---|---|
| Classifier | `galaxy-classifier-mi` | `<your-client-id>` |
| Scanner | `galaxyscanner-mi` | `<your-client-id>` |
| ASTAnalyzer | `galaxy-astanalyzer-mi` | `<your-client-id>` |
| Analyzer | `galaxy-analyzer-mi` | `<your-client-id>` |
| LambdaAnalyzer | `galaxy-lambdaanalyzer-mi` | `<your-client-id>` |
| Architect | `galaxy-architect-mi` | `<your-client-id>` |
| Coder | `galaxy-coder-mi` | `<your-client-id>` |
| Reviewer | `galaxy-reviewer-mi` | `<your-client-id>` |
| Security | `galaxy-security-mi` | `<your-client-id>` |
| SecurityReviewer | `galaxy-securityreviewer-mi` | `<your-client-id>` |
| Tester | `galaxy-tester-mi` | `<your-client-id>` |
| IaCGen | `galaxy-iacgen-mi` | `<your-client-id>` |
| SLOWatcher | `galaxy-slowatcher-mi` | `<your-client-id>` |
| DiscoveryScanner | `galaxy-discoveryscanner-mi` | `<your-client-id>` |
| DiscoveryGrapher | `galaxy-discoverygrapher-mi` | `<your-client-id>` |
| DiscoveryBRD | `galaxy-discoverybrd-mi` | `<your-client-id>` |
| DiscoveryArchitect | `galaxy-discoveryarchitect-mi` | `<your-client-id>` |
| DiscoveryStories | `galaxy-discoverystories-mi` | `<your-client-id>` |

---

## Not Yet Provisioned

| Resource | Blocker / Notes |
|---|---|
| PostgreSQL Flexible Server | DDL ready (`infra/ledger_schema.sql`) · hash-chain code wired · running in stdout mode until provisioned |
| VNet integration | Not required for current workload |
| GH Actions / Azure DevOps CI | No `.github/` directory — image push and job redeploy done manually via `scripts/provision_aca_jobs.sh` |

---

## Known Constraints

| Constraint | Impact |
|---|---|
| Account has `Contributor` but not `User Access Administrator` | Cannot create RBAC role assignments. ACR pull uses admin credentials (configured via Bicep), not `AcrPull` role grant. Key Vault is in access-policy mode. |
| APIM policy is portal-only | No policy XML in repo. Sub-key auth, headers guard, rate-limit, and AOAI key injection are live but cannot be reproduced from code alone. |
| ACA environment idles | Stops after ~20 min of inactivity; first API call restarts it (~5 min). First `az containerapp job start` after idle will fail — retry after 5 min. |
