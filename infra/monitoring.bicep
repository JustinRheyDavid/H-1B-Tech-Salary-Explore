// Alerting for the spend guardrail.
//
// This exists in Bicep rather than as a one-off CLI command because Step 12's
// teardown is `az group delete -n rg-h1b`, which destroys everything in the
// resource group. The budget itself is subscription-scoped and survives that,
// but it references this Action Group by resource ID — so a CLI-created group
// would leave the budget pointing at a dangling ID after any teardown, silently
// dropping the notification path that was actually tested.
//
// Recreating the environment must recreate the alerting with it.

@description('Email address that budget alerts are delivered to.')
param alertEmailAddress string

@description('Action Group name. Referenced by resource ID from infra/budget.json, so changing it means updating that file too.')
param actionGroupName string = 'ag-h1b-budget'

// Action Groups are always Global — they are not regional resources, and
// passing a region here fails.
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'Global'
  properties: {
    // Max 12 characters. Appears as the sender identifier in the alert.
    groupShortName: 'h1bbudget'
    enabled: true
    emailReceivers: [
      {
        name: 'primary'
        emailAddress: alertEmailAddress
        useCommonAlertSchema: false
      }
    ]
  }
}

output actionGroupId string = actionGroup.id
output actionGroupName string = actionGroup.name
