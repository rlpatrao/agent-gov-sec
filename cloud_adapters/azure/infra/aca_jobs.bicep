// cloud_adapters/azure/infra/aca_jobs.bicep
// Deploys one Container Apps Job per governance persona (finops · auditor · rogue).
//
// This is the Azure "Method 2 / fan-out" shape: each persona runs as its own job
// under its own User-Assigned Managed Identity (the persona's NHI), started by
// cloud_adapters/azure/orchestrator.py. It mirrors the AWS Batch orchestrator
// (cloud_adapters/aws/orchestrator.py) and the AWS per-agent IAM roles. The three
// personas match the AWS agent_types in cloud_adapters/aws/infra/main.tf.
//
// Deploy with:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file aca_jobs.bicep \
//     --parameters acrPassword=<acr-admin-password> storageAccountKey=<sa-key>

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Name of the existing Container Apps environment')
param environmentName string = 'galaxy-aca-env'

@description('ACR login server')
param registryServer string = 'examplecr.azurecr.io'

@description('ACR admin username')
param registryUsername string = 'examplecr'

@description('ACR admin password — passed at deploy time, never stored in source')
@secure()
param acrPassword string

@description('Container image tag to deploy')
param imageTag string = '0.3.0'

@description('Storage account name for the Azure Files artifact share')
param storageAccountName string = 'galaxysa'

@description('Azure Files share name for run artifacts')
param fileShareName string = 'galaxy-runs'

@description('Storage account key for the Azure Files mount — passed at deploy time')
@secure()
param storageAccountKey string

// ── Reference existing environment ───────────────────────────────────────────

resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

// ── Wire the Azure Files share to the environment ─────────────────────────────

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
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

// ── Persona definitions ───────────────────────────────────────────────────────
// name:      short slug used in resource names (no uppercase, no spaces)
// agentType: value injected as AGENT_TYPE env var (matches NHIRegistry keys)
// miName:    User-Assigned MI resource name (the persona's NHI — see main.bicep)

var personas = [
  { name: 'finops',  agentType: 'finops',  miName: 'galaxy-finops-mi' }
  { name: 'auditor', agentType: 'auditor', miName: 'galaxy-auditor-mi' }
  { name: 'rogue',   agentType: 'rogue',   miName: 'galaxy-rogue-mi' }
]

// ── Create one Container Apps Job per persona ─────────────────────────────────

resource jobs 'Microsoft.App/jobs@2024-03-01' = [for p in personas: {
  dependsOn: [ envStorage ]
  name: 'galaxy-${p.name}-job'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', p.miName)}': {}
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
        { name: 'acr-password', value: acrPassword }
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
          image: '${registryServer}/galaxy-agent:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AGENT_TYPE', value: p.agentType }
            { name: 'CLOUD_PROVIDER', value: 'azure' }
          ]
          volumeMounts: [
            { volumeName: 'galaxy-runs', mountPath: '/data' }
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

output jobNames array = [for (p, i) in personas: jobs[i].name]
