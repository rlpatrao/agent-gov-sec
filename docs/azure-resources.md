# Azure Resources — Galaxy SDLC Platform

> Last updated: 2026-05-22 · Verified against `az resource list --resource-group galaxyscanner-rg`
> None of these values are secrets. Client IDs and resource names are safe to store in source control.

---

## Subscription

| | |
|---|---|
| Name | AI Labs (801758) |
| Subscription ID | `8aee075f-c478-4da6-872c-ebcfef7a11c6` |
| Tenant ID | `0d85160c-5899-44ca-acc8-db1501b993b6` |
| Resource Group | `galaxyscanner-rg` · East US |

---

## Core Platform Services

| Resource | Name | Status | Key identifiers / notes |
|---|---|---|---|
| Key Vault | `galaxyscanner-kv-d63cdd` | ✅ Live | `https://galaxyscanner-kv-d63cdd.vault.azure.net/` · access-policy mode (no RBAC — account lacks `roleAssignments/write`) |
| Azure Container Registry | `galaxyscannercrd63cdd` | ✅ Live | `galaxyscannercrd63cdd.azurecr.io` · Basic SKU · admin creds enabled · image `galaxy-scanner:0.2.1` pushed |
| Azure OpenAI | `galaxyscanner-openai` | ✅ Live | Deployment: `gpt-5-3-codex` · Responses API (`/openai/v1/responses?api-version=preview`) · Chat Completions not supported for this model |
| APIM | `galaxyscanner-apim` | ✅ Live | Consumption tier · sub-key + required-headers guard + 100 RPM + AOAI key injection · portal-configured only (no policy XML in repo) |
| Log Analytics workspace | `galaxyscanner-law` | ✅ Live | customerId: `56bf830a-3691-49ba-a480-6717a4dbc22b` · linked to App Insights |
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

All 18 are real Entra Managed Identity principals in tenant `0d85160c-5899-44ca-acc8-db1501b993b6`.

| Agent | MI Name | Client ID |
|---|---|---|
| Classifier | `galaxy-classifier-mi` | `c4be541a-a1f2-433c-8166-9ebcf2d87b78` |
| Scanner | `galaxyscanner-mi` | `e581d9ea-c4ca-411f-9946-2e784d9c4046` |
| ASTAnalyzer | `galaxy-astanalyzer-mi` | `7d22106a-5fe0-467c-98f4-1080d8bcea4d` |
| Analyzer | `galaxy-analyzer-mi` | `8cdc89ee-932e-4536-a563-434af7df3c9b` |
| LambdaAnalyzer | `galaxy-lambdaanalyzer-mi` | `17de927d-a8d7-447b-90b7-d1d649009179` |
| Architect | `galaxy-architect-mi` | `7b2e5510-bbee-4da4-a99d-e60711fa0be7` |
| Coder | `galaxy-coder-mi` | `f51216a1-0e67-43c9-acb8-149954e8d4e0` |
| Reviewer | `galaxy-reviewer-mi` | `b44d54a3-d329-49aa-89cb-ea35522768ba` |
| Security | `galaxy-security-mi` | `72f1b573-1796-474e-b961-390ae8ad33fe` |
| SecurityReviewer | `galaxy-securityreviewer-mi` | `ae944f1a-1032-4cbb-ba53-8cb73a790043` |
| Tester | `galaxy-tester-mi` | `7eeb7e1a-b6f2-45d5-b721-2fa0b49da988` |
| IaCGen | `galaxy-iacgen-mi` | `72728f28-0955-4378-8782-cde5fdc6dff3` |
| SLOWatcher | `galaxy-slowatcher-mi` | `92f68691-ea09-4249-b9a1-221a5888c361` |
| DiscoveryScanner | `galaxy-discoveryscanner-mi` | `40d042bb-a23e-4158-92f8-70accc3023c7` |
| DiscoveryGrapher | `galaxy-discoverygrapher-mi` | `5a603c38-d178-4da4-94dd-85cedc9cd983` |
| DiscoveryBRD | `galaxy-discoverybrd-mi` | `333b400b-170a-4ed0-9fae-42866a93b84f` |
| DiscoveryArchitect | `galaxy-discoveryarchitect-mi` | `cc0da4ab-22fa-4707-8184-4e33c5884c3e` |
| DiscoveryStories | `galaxy-discoverystories-mi` | `26c11983-dad1-480e-bff8-09eb8f3ad7f0` |

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
