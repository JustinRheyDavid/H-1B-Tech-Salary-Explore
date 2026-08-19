// Entry point for the H-1B Tech Salary Explorer infrastructure.
//
// Deployed at resource group scope into rg-h1b. Every resource lives in
// canadacentral; rg-h1b's own location is eastus, but a resource group's
// location is metadata only and does not constrain its contents. Modules are
// added as the plan's phases land:
//
//   Step 2  monitoring.bicep      <- Action Group for budget alerts
//   Step 3  storage.bicep
//   Step 4  sql.bicep
//   Step 5  containerapps.bicep
//   Step 6  roles.bicep          <- blob role assignment (the SQL half is manual)
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

// Region is canadacentral, not the eastus originally settled in plan §9.2.
// eastus and eastus2 both return ProvisioningDisabled for Microsoft.Sql on this
// subscription — the SKU is in the catalog but not available to us:
//
//   az sql db list-editions -l <region> --edition GeneralPurpose --available
//
// Run that BEFORE choosing a region. Without --available it lists the global
// catalog and happily shows SKUs the subscription cannot actually provision,
// which is how eastus got picked and failed 90 seconds into a deployment.
//
// Deliberately NOT defaulted to resourceGroup().location: rg-h1b's metadata
// lives in eastus, and inheriting that is exactly the bug this comment exists
// to prevent. Resource group location is metadata only and does not affect
// where these resources run or what they cost.
@description('Azure region for every resource. canadacentral — see comment above.')
param location string

@description('Short project prefix, lowercase alphanumeric only.')
param namePrefix string = 'h1b'

@description('Days before a blob under raw/ is deleted.')
param rawRetentionDays int = 90

@description('Object ID (GUID) of the Entra principal to make SQL server admin.')
param entraAdminObjectId string

@description('UPN or display name of the Entra admin.')
param entraAdminLogin string

@description('Single IPv4 address allowed through the SQL firewall for local development. Empty by default — pass it at deploy time, do not commit it.')
param clientIpAddress string = ''

@description('Email address that budget alerts are delivered to.')
param alertEmailAddress string

module monitoring 'monitoring.bicep' = {
  name: 'monitoring'
  params: {
    alertEmailAddress: alertEmailAddress
  }
}

module storage 'storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    namePrefix: namePrefix
    rawRetentionDays: rawRetentionDays
  }
}

module sql 'sql.bicep' = {
  name: 'sql'
  params: {
    location: location
    // Logical SQL server names are globally unique across Azure but, unlike
    // storage accounts, may contain hyphens.
    //
    // `location` is part of the uniqueString seed on purpose. A SQL server name
    // is a global DNS name under .database.windows.net, and a FAILED creation
    // still registers it against the region it was attempted in. Retrying the
    // same name in a different region then fails with InvalidResourceLocation
    // even though `az sql server show` reports ResourceNotFound and the ARM
    // deployment history has been cleared — the hold is in DNS, not ARM, and
    // does not release on demand. Seeding with location means changing region
    // yields a new name and cannot collide with the abandoned one.
    sqlServerName: 'sql-${namePrefix}-${uniqueString(resourceGroup().id, location)}'
    databaseName: 'sqldb-${namePrefix}'
    entraAdminObjectId: entraAdminObjectId
    entraAdminLogin: entraAdminLogin
    clientIpAddress: clientIpAddress
  }
}

@description('Image for the Streamlit web app. Step 10 replaces the placeholder.')
param webImage string

@description('Port the web container listens on. Must match webImage — 80 for the placeholder, 8501 for the real Streamlit image.')
param webTargetPort int

@description('Image for the ETL job. Step 9 replaces the placeholder.')
param etlImage string

module containerapps 'containerapps.bicep' = {
  name: 'containerapps'
  params: {
    location: location
    namePrefix: namePrefix
    webImage: webImage
    webTargetPort: webTargetPort
    etlImage: etlImage
    // Passed rather than hardcoded in the app's environment: the FQDN carries a
    // per-deployment random suffix, so anyone redeploying into their own
    // subscription gets their own server without editing a container spec.
    // Referencing the outputs also orders the modules — SQL before the app that
    // points at it — without an explicit dependsOn.
    sqlServerFqdn: sql.outputs.sqlServerFqdn
    sqlDatabaseName: sql.outputs.databaseName
  }
}

// Depends on both modules above: the storage account to scope the assignment to,
// and the ETL job's identity to assign it to. Bicep infers the ordering from the
// output references, so no explicit dependsOn is needed.
//
// Covers only the storage half of Step 6. The Azure SQL grants cannot be
// expressed in ARM — see roles.bicep and sql/grant_identities.sql.
module roles 'roles.bicep' = {
  name: 'roles'
  params: {
    storageAccountName: storage.outputs.storageAccountName
    etlPrincipalId: containerapps.outputs.etlPrincipalId
  }
}

output storageAccountName string = storage.outputs.storageAccountName
output blobEndpoint string = storage.outputs.blobEndpoint
output sqlServerFqdn string = sql.outputs.sqlServerFqdn
output databaseName string = sql.outputs.databaseName
output webAppFqdn string = containerapps.outputs.webAppFqdn
output etlJobName string = containerapps.outputs.etlJobName

// Step 6 needs these to create the SQL users and assign the storage role.
output webPrincipalId string = containerapps.outputs.webPrincipalId
output etlPrincipalId string = containerapps.outputs.etlPrincipalId

// infra/budget.json hardcodes this ID in its contactGroups. If the Action Group
// is ever renamed or moved, that file must be updated and the budget re-applied.
output actionGroupId string = monitoring.outputs.actionGroupId
