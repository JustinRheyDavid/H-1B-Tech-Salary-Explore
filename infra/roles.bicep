// Data-plane role assignments for the managed identities.
//
// Plan Step 6 has two grant paths and they are not symmetric:
//
//   Storage   an Azure RBAC role assignment, declarable here.
//   Azure SQL NOT expressible in Bicep at all. Database users live inside the
//             database, not in ARM. They are created by `sql/grant_identities.sql`,
//             run by hand as the Entra admin, and they must be re-run if the
//             database is ever dropped and recreated.
//
// So this file covers exactly half of Step 6. The other half is manual by
// necessity, not by omission — see docs/azure-runbook.md §4, Step 6.
//
// This is a separate module rather than extra resources in storage.bicep. The
// role assignment needs the storage account (from the storage module) AND the
// ETL job's principal ID (from the containerapps module). Putting it in
// storage.bicep would make storage depend on containerapps, which inverts the
// natural order — nothing about a storage account should wait on an app. A
// module that depends on both keeps the arrows pointing one way.

@description('Name of the storage account the ETL job reads raw data from and writes curated data to.')
param storageAccountName string

@description('Principal ID of the h1b-etl job system-assigned managed identity. Read from the containerapps module output, not hardcoded — it changes if the job is deleted and recreated.')
param etlPrincipalId string

// Storage Blob Data Contributor. Built-in role IDs are fixed GUIDs, identical in
// every Azure tenant, so hardcoding this one is correct rather than lazy:
//
//   az role definition list --name "Storage Blob Data Contributor" --query "[].name" -o tsv
//
// This is a DATA-plane role and that distinction is the point of the whole file.
// Subscription Owner is a control-plane role: it lets you delete the storage
// account but not read a blob inside it. With `allowSharedKeyAccess: false` in
// storage.bicep there is no account-key fallback either, so without this
// assignment the ETL job's reads fail with AuthorizationPermissionMismatch even
// though its identity provably exists.
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

// Contributor, not Reader: the job reads from raw/ and writes to curated/.
// The web app deliberately gets NOTHING here — it reads from Azure SQL and has
// no reason to touch blobs at all.
resource etlBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  // A role assignment's name must be a GUID, and it is the assignment's
  // identity. Deriving it from (scope, principal, role) makes redeployment
  // idempotent: the same three inputs always produce the same name, so a second
  // deployment updates the existing assignment instead of failing with
  // RoleAssignmentExists or piling up duplicates.
  name: guid(storageAccount.id, etlPrincipalId, storageBlobDataContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: etlPrincipalId
    // Stating this avoids a real deployment failure rather than being
    // documentation. Without it ARM looks the principal up in Entra to infer the
    // type, and a just-created managed identity may not have replicated yet —
    // the deployment then fails with PrincipalNotFound on a principal that does
    // exist. Declaring the type skips the lookup.
    principalType: 'ServicePrincipal'
  }
}

output etlRoleAssignmentId string = etlBlobContributor.id
