// infra/aca_jobs.bicep
// Deploys one Container App Job per Galaxy agent (18 total).
// Each job has its own User-Assigned Managed Identity and mounts the shared
// Azure Files share at /data for artifact handoff between pipeline stages.
//
// Deploy with:
//   az deployment group create \
//     --resource-group galaxyscanner-rg \
//     --template-file infra/aca_jobs.bicep \
//     --parameters acrPassword=<acr-admin-password>

@description('Azure region for all resources')
param location string = 'eastus'

@description('Name of the existing Container Apps environment')
param environmentName string = 'galaxyscanner-aca-env'

@description('ACR login server')
param registryServer string = 'galaxyscannercrd63cdd.azurecr.io'

@description('ACR admin username')
param registryUsername string = 'galaxyscannercrd63cdd'

@description('ACR admin password — passed at deploy time, never stored in source')
@secure()
param acrPassword string

@description('Container image tag to deploy')
param imageTag string = '0.2.1'

@description('Storage account name for Azure Files artifact share')
param storageAccountName string = 'galaxyscannersa'

@description('Azure Files share name for run artifacts')
param fileShareName string = 'galaxy-runs'

@description('Storage account key for the Azure Files mount — passed at deploy time')
@secure()
param storageAccountKey string

// ── Reference existing environment ───────────────────────────────────────────

resource acaEnv 'Microsoft.App/managedEnvironments@2023-05-01' existing = {
  name: environmentName
}

// ── Wire Azure Files share to the environment ─────────────────────────────────

resource envStorage 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  name: fileShareName
  parent: acaEnv
  properties: {
    azureFile: {
      accountName: storageAccountName
      accountKey: storageAccountKey
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

// ── Agent definitions ─────────────────────────────────────────────────────────
// name: short slug used in resource names (no uppercase, no spaces)
// agentType: value injected as AGENT_TYPE env var (matches NHIRegistry keys)
// miName: existing User-Assigned MI resource name
// miClientId: Entra client ID (not a secret)

var agents = [
  { name: 'classifier',         agentType: 'Classifier',         miName: 'galaxy-classifier-mi',         miClientId: 'c4be541a-a1f2-433c-8166-9ebcf2d87b78' }
  { name: 'scanner',            agentType: 'Scanner',            miName: 'galaxyscanner-mi',              miClientId: 'e581d9ea-c4ca-411f-9946-2e784d9c4046' }
  { name: 'astanalyzer',        agentType: 'ASTAnalyzer',        miName: 'galaxy-astanalyzer-mi',         miClientId: '7d22106a-5fe0-467c-98f4-1080d8bcea4d' }
  { name: 'analyzer',           agentType: 'Analyzer',           miName: 'galaxy-analyzer-mi',            miClientId: '8cdc89ee-932e-4536-a563-434af7df3c9b' }
  { name: 'lambdaanalyzer',     agentType: 'LambdaAnalyzer',     miName: 'galaxy-lambdaanalyzer-mi',      miClientId: '17de927d-a8d7-447b-90b7-d1d649009179' }
  { name: 'architect',          agentType: 'Architect',          miName: 'galaxy-architect-mi',           miClientId: '7b2e5510-bbee-4da4-a99d-e60711fa0be7' }
  { name: 'coder',              agentType: 'Coder',              miName: 'galaxy-coder-mi',               miClientId: 'f51216a1-0e67-43c9-acb8-149954e8d4e0' }
  { name: 'reviewer',           agentType: 'Reviewer',           miName: 'galaxy-reviewer-mi',            miClientId: 'b44d54a3-d329-49aa-89cb-ea35522768ba' }
  { name: 'security',           agentType: 'Security',           miName: 'galaxy-security-mi',            miClientId: '72f1b573-1796-474e-b961-390ae8ad33fe' }
  { name: 'securityreviewer',   agentType: 'SecurityReviewer',   miName: 'galaxy-securityreviewer-mi',    miClientId: 'ae944f1a-1032-4cbb-ba53-8cb73a790043' }
  { name: 'tester',             agentType: 'Tester',             miName: 'galaxy-tester-mi',              miClientId: '7eeb7e1a-b6f2-45d5-b721-2fa0b49da988' }
  { name: 'iacgen',             agentType: 'IaCGen',             miName: 'galaxy-iacgen-mi',              miClientId: '72728f28-0955-4378-8782-cde5fdc6dff3' }
  { name: 'slowatcher',         agentType: 'SLOWatcher',         miName: 'galaxy-slowatcher-mi',          miClientId: '92f68691-ea09-4249-b9a1-221a5888c361' }
  { name: 'discoveryscanner',   agentType: 'DiscoveryScanner',   miName: 'galaxy-discoveryscanner-mi',    miClientId: '40d042bb-a23e-4158-92f8-70accc3023c7' }
  { name: 'discoverygrapher',   agentType: 'DiscoveryGrapher',   miName: 'galaxy-discoverygrapher-mi',    miClientId: '5a603c38-d178-4da4-94dd-85cedc9cd983' }
  { name: 'discoverybrd',       agentType: 'DiscoveryBRD',       miName: 'galaxy-discoverybrd-mi',        miClientId: '333b400b-170a-4ed0-9fae-42866a93b84f' }
  { name: 'discoveryarchitect', agentType: 'DiscoveryArchitect', miName: 'galaxy-discoveryarchitect-mi',  miClientId: 'cc0da4ab-22fa-4707-8184-4e33c5884c3e' }
  { name: 'discoverystories',   agentType: 'DiscoveryStories',   miName: 'galaxy-discoverystories-mi',    miClientId: '26c11983-dad1-480e-bff8-09eb8f3ad7f0' }
]

// ── Create one Container App Job per agent ────────────────────────────────────

resource jobs 'Microsoft.App/jobs@2023-05-01' = [for agent in agents: {
  dependsOn: [envStorage]
  name: 'galaxy-${agent.name}-job'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', agent.miName)}': {}
    }
  }
  properties: {
    environmentId: acaEnv.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 3600
      replicaRetryLimit: 1
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: [
        {
          name: 'acr-password'
          value: acrPassword
        }
      ]
      registries: [
        {
          server: registryServer
          username: registryUsername
          passwordSecretRef: 'acr-password'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: '${registryServer}/galaxy-scanner:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AGENT_TYPE',    value: agent.agentType }
            { name: 'NHI_CLIENT_ID', value: agent.miClientId }
          ]
          volumeMounts: [
            {
              volumeName: 'galaxy-runs'
              mountPath: '/data'
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'galaxy-runs'
          storageType: 'AzureFile'
          storageName: fileShareName
        }
      ]
    }
  }
}]

// ── Outputs ───────────────────────────────────────────────────────────────────

output jobNames array = [for (agent, i) in agents: jobs[i].name]
