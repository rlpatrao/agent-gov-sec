# Azure resources for Galaxy Scanner

Generated 2026-04-23. Keep alongside code; none of these are secrets.

## Subscription
- Name: AI Labs (801758)
- ID: `8aee075f-c478-4da6-872c-ebcfef7a11c6`
- Tenant: Virtusa (`0d85160c-5899-44ca-acc8-db1501b993b6`)

## Phase 1 (foundation)
| Resource | Name | Key identifier |
|---|---|---|
| Resource Group | `galaxyscanner-rg` | eastus |
| User-Assigned MI (Scanner NHI) | `galaxyscanner-mi` | clientId: `e581d9ea-c4ca-411f-9946-2e784d9c4046` <br> principalId: `d82c20db-6160-4b5a-93d6-0a2cbbc1fa26` |
| Key Vault (access-policy mode) | `galaxyscanner-kv-d63cdd` | URI: `https://galaxyscanner-kv-d63cdd.vault.azure.net/` |
| Azure Container Registry (Basic) | `galaxyscannercrd63cdd` | Login: `galaxyscannercrd63cdd.azurecr.io` |
| Log Analytics workspace | `galaxyscanner-law` | customerId: `56bf830a-3691-49ba-a480-6717a4dbc22b` |

## Existing (pre-created)
| Resource | Name | Notes |
|---|---|---|
| Microsoft Foundry | `ailab-solution-agentic-sdlc` | East US; Claude model deployment TBD (Phase 1.5) |

## Env vars for Container Apps deploy (Phase 4)
```
AZURE_KEY_VAULT_URL=https://galaxyscanner-kv-d63cdd.vault.azure.net/
NHI_CLIENT_ID_SCANNER=e581d9ea-c4ca-411f-9946-2e784d9c4046
OTEL_SERVICE_NAME=galaxy-platform
# AZURE_FOUNDRY_ENDPOINT=... (set after model deployment)
# POSTGRES_DSN=... (set after Phase 2)
# OTEL_EXPORTER_OTLP_ENDPOINT=... (App Insights connection string in Phase 2)
```

## Known enterprise constraints
- Account `rpatrao@virtusa.com` has Contributor but NOT User Access Administrator — can create resources but can't grant RBAC.
- Key Vault is in legacy access-policy mode because of above; Workload Identity works fine against either mode.
- ACR "AcrPull" grant for Container App MI (Phase 4) will hit the same RBAC wall; use ACR scope-map tokens as workaround.
