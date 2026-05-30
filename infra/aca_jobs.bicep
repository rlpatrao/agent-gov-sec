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
  { name: 'classifier',         agentType: 'Classifier',         miName: 'galaxy-classifier-mi',         miClientId: '' }
  { name: 'scanner',            agentType: 'Scanner',            miName: 'galaxyscanner-mi',              miClientId: '' }
  { name: 'astanalyzer',        agentType: 'ASTAnalyzer',        miName: 'galaxy-astanalyzer-mi',         miClientId: '' }
  { name: 'analyzer',           agentType: 'Analyzer',           miName: 'galaxy-analyzer-mi',            miClientId: '' }
  { name: 'lambdaanalyzer',     agentType: 'LambdaAnalyzer',     miName: 'galaxy-lambdaanalyzer-mi',      miClientId: '' }
  { name: 'architect',          agentType: 'Architect',          miName: 'galaxy-architect-mi',           miClientId: '' }
  { name: 'coder',              agentType: 'Coder',              miName: 'galaxy-coder-mi',               miClientId: '' }
  { name: 'reviewer',           agentType: 'Reviewer',           miName: 'galaxy-reviewer-mi',            miClientId: '' }
  { name: 'security',           agentType: 'Security',           miName: 'galaxy-security-mi',            miClientId: '' }
  { name: 'securityreviewer',   agentType: 'SecurityReviewer',   miName: 'galaxy-securityreviewer-mi',    miClientId: '' }
  { name: 'tester',             agentType: 'Tester',             miName: 'galaxy-tester-mi',              miClientId: '' }
  { name: 'iacgen',             agentType: 'IaCGen',             miName: 'galaxy-iacgen-mi',              miClientId: '' }
  { name: 'slowatcher',         agentType: 'SLOWatcher',         miName: 'galaxy-slowatcher-mi',          miClientId: '' }
  { name: 'discoveryscanner',   agentType: 'DiscoveryScanner',   miName: 'galaxy-discoveryscanner-mi',    miClientId: '' }
  { name: 'discoverygrapher',   agentType: 'DiscoveryGrapher',   miName: 'galaxy-discoverygrapher-mi',    miClientId: '' }
  { name: 'discoverybrd',       agentType: 'DiscoveryBRD',       miName: 'galaxy-discoverybrd-mi',        miClientId: '' }
  { name: 'discoveryarchitect', agentType: 'DiscoveryArchitect', miName: 'galaxy-discoveryarchitect-mi',  miClientId: '' }
  { name: 'discoverystories',   agentType: 'DiscoveryStories',   miName: 'galaxy-discoverystories-mi',    miClientId: '' }
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
