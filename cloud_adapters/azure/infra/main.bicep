// cloud_adapters/azure/infra/main.bicep — reference topology for the Azure deployment.
//
// The Azure counterpart of cloud_adapters/aws/infra/main.tf. Provisions everything
// `demo_agents.py --azure` needs to run against a real model through a governed
// APIM → Azure OpenAI egress chokepoint, plus the out-of-process Function
// chokepoints (llm / data / a2a):
//
//   - per-persona NHI identities (galaxy-<persona>-mi)  — user-assigned Managed Identity
//   - hash-chain ledger (PostgreSQL Flexible Server)    — PostgresHashChainBackend
//   - Key Vault (galaxy-<suffix>-kv)                    — AOAI key + gateway sub-key + PG password
//   - Function App (galaxy-<suffix>-func)               — llm/data/a2a chokepoints
//   - API Management (galaxy-<suffix>-apim)             — the "apim" egress edge
//   - Log Analytics + Application Insights              — OTel span sink (AzureTraceExporterFactory)
//
// This is a *reference* topology — deploy against your own subscription/region.
// Azure OpenAI model access must be enabled for the tenant first.
//
//   az deployment group create -g <rg> -f main.bicep \
//     -p aoaiKey=<key> pgAdminPassword=<pw>
//
// Outputs wire straight into .env (APIM endpoint, Key Vault URL, PG DSN host, MI client ids).

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Short suffix appended to globally-unique resource names')
param nameSuffix string = uniqueString(resourceGroup().id)

@description('The three governance demo personas (NHI per persona) — matches the AWS agent_types')
param personas array = [ 'finops', 'auditor', 'rogue' ]

@description('Azure OpenAI endpoint the APIM edge and Function proxy call')
param aoaiEndpoint string = 'https://<your-openai>.openai.azure.com/'

@description('Azure OpenAI API key — stored in Key Vault, injected at the APIM edge; never returned to callers')
@secure()
param aoaiKey string

@description('PostgreSQL administrator password for the ledger server')
@secure()
param pgAdminPassword string

@description('PostgreSQL administrator login')
param pgAdminLogin string = 'galaxyadmin'

var prefix = 'galaxy-${nameSuffix}'
var tags = {
  project: 'galaxy-rp'
  managed_by: 'bicep'
  component: 'agent-gov-demo'
}

// ── Observability: Log Analytics + Application Insights ──────────────────────
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-law'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-ai'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
  }
}

// ── Per-persona NHI: one user-assigned Managed Identity per persona ──────────
// The MI clientId is the persona's NHI id; wire it into .env as NHI_CLIENT_ID_<PERSONA>.
resource personaMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for p in personas: {
  name: 'galaxy-${p}-mi'
  location: location
  tags: tags
}]

// ── Key Vault: AOAI key + gateway subscription key + PG password ─────────────
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${take(replace(prefix, '-', ''), 20)}kv'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: false
    // Access-policy mode. The Function App grant is a separate accessPolicies
    // child (below) to avoid a keyVault↔functionApp dependency cycle.
    accessPolicies: []
  }
}

// Grant the Function App's system-assigned identity secret read (add-mode policy,
// declared separately so Key Vault does not have to reference the Function App).
resource kvFuncAccess 'Microsoft.KeyVault/vaults/accessPolicies@2023-07-01' = {
  parent: keyVault
  name: 'add'
  properties: {
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: functionApp.identity.principalId
        permissions: { secrets: [ 'get', 'list' ] }
      }
    ]
  }
}

resource aoaiSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'azure-openai-key'
  properties: { value: aoaiKey }
}

resource pgSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'postgres-password'
  properties: { value: pgAdminPassword }
}

resource aiConnSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'appinsights-connection-string'
  properties: { value: appInsights.properties.ConnectionString }
}

// ── Storage account (Functions backing store) ────────────────────────────────
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${take(replace(prefix, '-', ''), 18)}sa'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

// ── Function App (the llm / data / a2a chokepoints) ──────────────────────────
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${prefix}-plan'
  location: location
  tags: tags
  sku: { name: 'Y1', tier: 'Dynamic' }
  properties: { reserved: true }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: '${prefix}-func'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.12'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storage.listKeys().keys[0].value}' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'AZURE_OPENAI_ENDPOINT', value: aoaiEndpoint }
        { name: 'AZURE_KEY_VAULT_URL', value: keyVault.properties.vaultUri }
        { name: 'GOV_POLICY_REGISTRY_PATH', value: '/home/site/wwwroot/agent-controls.json' }
      ]
    }
  }
}

// ── PostgreSQL Flexible Server (hash-chain trace ledger) ─────────────────────
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${prefix}-pg'
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: pgAdminLogin
    administratorLoginPassword: pgAdminPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: 'ledger'
}

// ── API Management (the "apim" egress edge) ──────────────────────────────────
resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' = {
  name: '${prefix}-apim'
  location: location
  tags: tags
  sku: { name: 'Consumption', capacity: 0 }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherName: 'Galaxy Agentic Governance'
    publisherEmail: 'governance@example.com'
  }
}

resource apimApi 'Microsoft.ApiManagement/service/apis@2023-05-01-preview' = {
  parent: apim
  name: 'aoai-egress'
  properties: {
    displayName: 'Azure OpenAI egress (governed)'
    path: 'openai'
    protocols: [ 'https' ]
    serviceUrl: aoaiEndpoint
    subscriptionRequired: true
  }
}

resource apimPolicy 'Microsoft.ApiManagement/service/apis/policies@2023-05-01-preview' = {
  parent: apimApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('apim-policy.xml')
  }
}

// ── Outputs (wire these into .env) ───────────────────────────────────────────
output apimEndpoint string = '${apim.properties.gatewayUrl}/openai'
output functionAppName string = functionApp.name
output keyVaultUrl string = keyVault.properties.vaultUri
output ledgerHost string = pg.properties.fullyQualifiedDomainName
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output personaClientIds array = [for (p, i) in personas: { persona: p, clientId: personaMi[i].properties.clientId }]
