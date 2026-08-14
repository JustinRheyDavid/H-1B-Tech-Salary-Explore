// Azure SQL Database on the permanent free offer.
//
// This is the resource where a wrong flag costs real money, so every value the
// plan calls out is set explicitly rather than left to a default:
//
//   useFreeLimit                true    opts into the free vCore/storage grant
//   freeLimitExhaustionBehavior AutoPause  pause when the grant runs out...
//                                          ...NOT BillOverUsage, which charges
//   autoPauseDelay              60      minutes idle before pausing
//   minCapacity                 0.5     smallest serverless floor
//   maxSizeBytes                32 GiB  the free storage allowance
//
// Only ONE free-offer database is allowed per subscription. If a second is ever
// created, the deployment fails rather than silently billing.

@description('Azure region.')
param location string

@description('Logical SQL server name. Globally unique, lowercase.')
param sqlServerName string

@description('Database name.')
param databaseName string

@description('Object ID (GUID) of the Entra principal to make server admin.')
param entraAdminObjectId string

@description('UPN or display name of the Entra admin. Shown in the portal; the sid is what actually grants access.')
param entraAdminLogin string

@description('Single IPv4 address to allow through the firewall for local development. Left empty by default on purpose — a home IP address does not belong in a public repository. Pass it at deploy time instead.')
param clientIpAddress string = ''

// SQL authentication is disabled outright: `azureADOnlyAuthentication: true`
// and no administratorLogin/administratorLoginPassword pair anywhere. That makes
// password-based access impossible rather than merely discouraged, and means
// there is no credential in this template to leak.
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  properties: {
    version: '12.0'
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      principalType: 'User'
      login: entraAdminLogin
      sid: entraAdminObjectId
      tenantId: subscription().tenantId
      azureADOnlyAuthentication: true
    }
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: databaseName
  location: location
  // The plan calls this SKU "GP_S_Gen5_2", and `az sql db show` reports exactly
  // that as currentServiceObjectiveName. But ARM stores it split: the vCore
  // count lives in `capacity`, and `name` keeps only the `GP_S_Gen5` stem.
  // Writing 'GP_S_Gen5_2' here alongside `capacity: 2` states the vCore count
  // twice, and `what-if` then reports a permanent
  //   sku.name: "GP_S_Gen5" => "GP_S_Gen5_2"
  // modification against infrastructure that is already correct.
  sku: {
    name: 'GP_S_Gen5'
    tier: 'GeneralPurpose'
    family: 'Gen5'
    capacity: 2
  }
  properties: {
    // The free grant. AutoPause is the whole reason this project can promise
    // $0.00 — on exhaustion the database stops rather than billing over.
    useFreeLimit: true
    freeLimitExhaustionBehavior: 'AutoPause'

    autoPauseDelay: 60
    minCapacity: json('0.5')
    maxSizeBytes: 34359738368 // 32 GiB

    // Geo-redundant backup storage is billable beyond the free allowance and
    // buys nothing for a portfolio database that can be rebuilt from DOL files.
    requestedBackupStorageRedundancy: 'Local'
    zoneRedundant: false

    // Database-level collation. Note that §6's `titles` table needs a BIN2
    // collation on the column itself — 9,286 of 123,990 titles collide under
    // case-insensitive comparison and the UNIQUE constraint rejects them. That
    // belongs in the DDL, not here.
    collation: 'SQL_Latin1_General_CP1_CI_AS'
  }
}

// Lets other Azure services (the Container App and ETL job) reach the server.
// The 0.0.0.0-0.0.0.0 range is a documented Azure special case meaning "Azure
// internal traffic", not "the entire internet".
resource allowAzureServices 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sqlServer
  name: 'AllowAllWindowsAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// Only created when an address is supplied at deploy time. See the parameter
// description for why there is no default.
resource allowClientIp 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = if (!empty(clientIpAddress)) {
  parent: sqlServer
  name: 'ClientDevelopmentMachine'
  properties: {
    startIpAddress: clientIpAddress
    endIpAddress: clientIpAddress
  }
}

output sqlServerName string = sqlServer.name
output sqlServerFqdn string = sqlServer.properties.fullyQualifiedDomainName
output databaseName string = sqlDatabase.name
