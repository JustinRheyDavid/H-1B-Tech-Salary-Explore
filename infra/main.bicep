// Entry point for the H-1B Tech Salary Explorer infrastructure.
//
// Deployed at resource group scope into rg-h1b (eastus). Modules are added as
// the plan's phases land:
//
//   Step 3  storage.bicep         <- this deployment
//   Step 4  sql.bicep
//   Step 5  containerapps.bicep
//
// Deploy:
//   az deployment group create -g rg-h1b --template-file infra/main.bicep \
//     --parameters infra/main.parameters.json \
//     --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
//
// Re-running must be a no-op. Bicep deployments are declarative, so a second
// run with unchanged parameters reports no changes rather than recreating
// anything — that idempotence is Step 3's acceptance criterion.

targetScope = 'resourceGroup'

@description('Azure region for every resource. Settled as eastus in plan §9.2.')
param location string = resourceGroup().location

@description('Short project prefix, lowercase alphanumeric only.')
param namePrefix string = 'h1b'

@description('Days before a blob under raw/ is deleted.')
param rawRetentionDays int = 90

module storage 'storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    namePrefix: namePrefix
    rawRetentionDays: rawRetentionDays
  }
}

output storageAccountName string = storage.outputs.storageAccountName
output blobEndpoint string = storage.outputs.blobEndpoint
