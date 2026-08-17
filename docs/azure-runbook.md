# Azure runbook — H-1B Tech Salary Explorer

Operational notes for the Azure deployment (Phase 2). How to stand it up, how to
check what it costs, and how to tear it down.

**Status:** Steps 1–5 done. Every value below was read from a live `az` command
or a real SQL connection rather than transcribed. The data path (Step 7 onward)
and teardown are still `<TO BE WRITTEN>`; any remaining `<FILL IN>` is a real
blank, not a placeholder for something already known.

The build plan this follows is [`docs/plans/azure-migration.md`](plans/azure-migration.md).

---

## 0. Settled decisions

Both get baked into a federated credential (Step 11) and a manual T-SQL grant
(Step 6), where changing them is annoying. Decided 2026-08-14, before any
resource existed.

### 0.1 Region — `canadacentral` (revised 2026-08-14)

```
REGION = canadacentral
```

Originally `eastus`, per plan §9.2's reasoning about widest service availability.
**That turned out to be wrong for this subscription.** Azure SQL returns
`ProvisioningDisabled` in both `eastus` and `eastus2` here — provisioning is
restricted, and the only remedies Azure offers are a different region or a
support request.

Check this **before** picking a region, not after a failed deployment:

```bash
az sql db list-editions -l <region> --edition GeneralPurpose --available --query "[].supportedServiceLevelObjectives[?name=='GP_S_Gen5_2']"
```

`--available` is the whole point. Without it the command lists Azure's global
catalog, which happily includes SKUs this subscription cannot provision. Probed
2026-08-14:

| Region | In catalog | Available to this subscription |
|---|---|---|
| `eastus` | yes | **no** |
| `eastus2` | yes | **no** |
| `canadacentral` | yes | yes |
| `westus2` | yes | yes |
| `centralus` | yes | yes |

`canadacentral` chosen from the available set — it is the closest to Montreal,
so lowest latency for local use and for Canadian reviewers. Container Apps and
Storage were confirmed available there too before committing.

> **`rg-h1b` itself still reports `eastus`.** A resource group's location only
> determines where its *metadata* is stored; it does not constrain or affect
> where resources run, what they cost, or their latency. Every actual resource
> is in `canadacentral`. This is why `main.bicep` takes `location` as a required
> parameter instead of defaulting to `resourceGroup().location` — inheriting it
> would silently put everything back in the region that does not work.

### 0.2 Resource naming — `h1b-*`

```
RESOURCE_GROUP = rg-h1b
NAME_PREFIX    = h1b-
```

| Resource | Name |
|---|---|
| Resource group | `rg-h1b` |
| Web container app | `h1b-web` |
| ETL container job | `h1b-etl` |
| SQL database | `sqldb-h1b` |

Resolves the conflict between Step 1 (which said `rg-prevailing`) and §9.6
(which recommended renaming). "Prevailing" was a working title the project never
actually used — it is the H-1B Tech Salary Explorer in the repo, the page title,
and the README, and the Azure resource names were the last place the old name
survived. The plan has been amended to match.

> **Do not** rename the `prevailing_wage` column in the schema. It is a DOL data
> field — the wage floor an employer must pay — and has nothing to do with the
> discarded project name. Renaming it breaks the T-SQL DDL against Phase 1's
> data.

---

## 1. Account setup

### Use a personal Microsoft account, not a school or work one

**This is not a style preference — a school/work tenant will block Step 11.**

The first attempt at this used a university account. It got as far as creating
the resource group before the blocker showed up:

```
allowedToCreateApps: false
```

Most institutional tenants forbid regular users from creating Entra app
registrations. Step 11's GitHub Actions OIDC design needs one, and so does the
service-principal fallback — both blocked. The failure does not surface at
signup; it surfaces at token exchange, several steps later, as `AADSTS700213`,
which does not name the real cause.

A university tenant has two further problems: your access disappears when you
graduate, and your personal subscription sits next to institutional production
subscriptions where one stray `az account set` does real damage.

So: sign up with a **personal** Microsoft account (outlook.com, gmail, etc.).
You become Global Admin of your own tenant and none of the above applies.

### Pre-flight check — run this before building anything

After `az login`, confirm the tenant can actually host this project:

```bash
az rest --method get --url "https://graph.microsoft.com/v1.0/policies/authorizationPolicy" --query "defaultUserRolePermissions.allowedToCreateApps"
```

**Must return `true`.** If it returns `false`, stop — Step 11 cannot be
completed in this tenant, and finding that out now costs nothing while finding
it out at Step 11 costs the whole CI/CD story.

### What only a human can do

Account creation is not automatable from a terminal and not delegable to an
agent. In a browser, at https://azure.microsoft.com/free:

1. Sign up for a free Azure account with a personal Microsoft account.
2. **A credit card is required at signup.** Azure verifies it with a temporary
   authorization (typically ~$1, reversed). This is true even though everything
   in this plan is designed to cost $0.00.
3. The account starts as a **Free Trial** subscription with trial credit that
   expires in ~30 days. See §2.1 — this matters more than the plan originally
   assumed.

Then authenticate the CLI:

```bash
az login
```

### Record the identifiers

Run this and paste the real output below — do not transcribe from the portal by
hand, and do not fill these in from memory:

```bash
az account show --query "{subscriptionId:id, tenantId:tenantId, name:name, state:state}" -o table
```

```
SUBSCRIPTION_ID   = 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
TENANT_ID         = 304bb5f5-a80d-4da5-8732-678bbb2888ed
SUBSCRIPTION_NAME = Azure subscription 1
ACCOUNT           = personal Microsoft account (gmail)
OFFER             = PayAsYouGo_2014-09-01, spending limit Off
```

> These are not secrets in the credential sense — a subscription ID is not a
> password, and it appears in every resource ID and ARM template. But this file
> is in a public repo. If that ever feels uncomfortable, move this block to a
> local untracked file and leave the commands here.

### Always pin the subscription by ID, never by name

**Three subscriptions are visible from this machine, and two share a name.**

| Name | Tenant | What it is |
|---|---|---|
| `Azure subscription 1` | `304bb5f5…` | **This project.** Personal account. |
| `Azure subscription 1` | `5569f185…` | University tenant — abandoned, see above |
| `iits-dsg-wvdservices-prd` | `5569f185…` | **University WVD production. Never touch.** |

So this command is ambiguous and must never be used:

```bash
az account set --subscription "Azure subscription 1"   # DO NOT - matches two
```

Pass `--subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3` explicitly on every
`az group`, `az deployment`, and teardown command. A Bicep deploy that inherits
the wrong default lands in a university production environment.

Renaming this subscription to something unique (portal → Subscriptions → Rename)
would remove the ambiguity at the source, and is worth doing.

### Create the resource group

```bash
az group create --name rg-h1b --location eastus --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

A resource group is free and holds nothing, so creating it before the budget in
§2 is safe. **Nothing else is** — and with the spending limit Off, that is not a
formality. See §2.1.

### Step 1 acceptance — DONE 2026-08-14

```bash
az account show
az group show -n rg-h1b --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

- [x] `az account show` returns the subscription
- [x] `az group show` returns the group — `rg-h1b`, `Succeeded` (metadata in `eastus`; resources are in `canadacentral`, see §0.1)
- [x] Subscription ID and tenant ID recorded above
- [x] Pre-flight check passed — `allowedToCreateApps: true`, so Step 11 is viable
- [x] Offer type checked — Pay-As-You-Go, **no spending limit**, see §2.1

---

## 2. Spend guardrails

**Do not start Phase B (Step 3, the first Bicep deploy) until this section is
complete.** That ordering is the whole point — a guardrail added after the
resources exist has already failed at the one job it had.

Step 2 states the gate as "an alert email has actually arrived." That is not
achievable here; see *Verify the alert* below for why, and for what was
verified in its place.

### 2.1 Free Trial vs Pay-As-You-Go — know which you are on

Check it:

```bash
az account show --query id -o tsv | xargs -I{} az rest --method get --url "https://management.azure.com/subscriptions/{}?api-version=2020-01-01" --query "{quotaId:subscriptionPolicies.quotaId, spendingLimit:subscriptionPolicies.spendingLimit}"
```

| | Free Trial (`FreeTrial_*`) | Pay-As-You-Go |
|---|---|---|
| Spending limit | **On** — Azure disables resources instead of charging | **Removed** — real charges possible |
| Can you be billed? | No | Yes |
| Lifetime | Subscription is **disabled after ~30 days** unless upgraded | Indefinite |

The plan originally assumed that trial credit expiring was harmless, since every
resource targets a permanent free tier. That is not quite right. On a Free Trial
the **subscription itself** switches off, and a permanent free tier does not
help if the subscription hosting it is disabled.

So the trial ending forces a choice, and the safe-looking option is the one that
removes your safety net:

- **Upgrade to Pay-As-You-Go** — everything keeps running on the permanent free
  tiers, but the spending limit is gone and the §2 budget alert becomes the
  *only* guardrail. Note that budget alerts **notify; they do not cap.** Nothing
  after the upgrade stops a billable resource except discipline.
- **Do not upgrade** — the subscription is disabled and the Azure demo link dies.
  Phase 1 on Streamlit Cloud is unaffected, which is exactly why plan assumption
  B6 keeps it live.

### This subscription is Pay-As-You-Go with the spending limit OFF

Checked 2026-08-14:

```
quotaId:       PayAsYouGo_2014-09-01
spendingLimit: Off
```

The free trial was not offered — Microsoft grants it once per person/payment
instrument, and it had already been used on the abandoned university tenant. So
there is no trial credit and **no trial expiry to plan around**, which removes
the 30-day cliff entirely. The permanent free tiers this project targets apply
to Pay-As-You-Go exactly as they do to a trial.

The trade is that **nothing technical prevents a bill.** There is no spending
limit to disable resources on your behalf. That makes §2's budget the only
guardrail, and budget alerts **notify; they do not cap.**

Two consequences, both load-bearing:

1. The budget in §2 is not a formality. Do it before any billable resource
   exists, and confirm the email actually arrives.
2. Assumption B8 (GHCR over ACR) and the `minReplicas: 0` setting are now the
   difference between $0.00 and a real charge, rather than between $0.00 and a
   credit drawdown. Re-read §7 of the plan before Phase B.

### The budget

A **$1.00** monthly budget on the subscription, with alerts at 50%, 80%, and
100% to your email.

A $1 budget on a project designed to cost $0.00 means *any* alert at all is a
real signal rather than noise. Do not raise it to a "safer" round number — that
inverts the logic and makes the alert meaningless.

```
BUDGET_NAME   = h1b-zero-spend
BUDGET_AMOUNT = 1.00
TIME_GRAIN    = Monthly
ALERT_EMAIL   = justinrheydavid@gmail.com
THRESHOLDS    = 50% / 80% / 100%, all Actual (not Forecast)
CREATED_VIA   = Cost Management API (PUT .../Microsoft.Consumption/budgets/h1b-zero-spend)
CREATED_ON    = 2026-08-14
```

Recreate it from scratch with:

```bash
az rest --method put --url "https://management.azure.com/subscriptions/54d2e1cd-805a-4c5e-ac6f-25932378fcd3/providers/Microsoft.Consumption/budgets/h1b-zero-spend?api-version=2021-10-01" --headers "Content-Type=application/json" --body @infra/budget.json
```

### Verify the alert — cannot be done by spending

An unverified alert is not a guardrail. But this one **cannot be verified the
way Step 2 describes**, and that is not a scheduling problem to work around.

A budget threshold fires when spend exceeds a percentage of the budget amount.
This project is engineered to cost exactly $0.00. Zero does not exceed any
positive percentage of any positive budget amount, so **no threshold can ever be
crossed while the design is working correctly.** Lowering the budget does not
help: 50% of $0.01 is $0.005, and $0.00 still does not exceed it.

Confirmed empirically 2026-08-14: after Step 3 deployed a real storage account,
`currentSpend` still read `0.0`. An empty storage account costs nothing, so
deploying infrastructure does not produce testable spend either.

**Step 2's acceptance criterion as written is unachievable for this project.**
The only condition that would satisfy it is the project failing at its goal.

### What was verified instead — the delivery path

What actually matters is whether an alert would *reach you* if spend appeared.
That is testable without spending anything, by routing the budget through an
Action Group and using its built-in test notification.

```
ACTION_GROUP      = ag-h1b-budget  (short name: h1bbudget)
RECEIVER          = primary -> justinrheydavid@gmail.com, status Enabled
ATTACHED_TO       = h1b-zero-spend, all three thresholds, via contactGroups
TEST_SENT         = 2026-08-14 19:43:46 UTC
TEST_RESULT       = Succeeded (MechanismType Email, state Complete)
```

Recreate the Action Group and re-run the test with:

```bash
az monitor action-group create --name ag-h1b-budget --resource-group rg-h1b --short-name h1bbudget --action email primary justinrheydavid@gmail.com --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

```bash
az monitor action-group test-notifications create --action-group ag-h1b-budget --resource-group rg-h1b --alert-type budget --add-action email primary justinrheydavid@gmail.com --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

> The test command polls for delivery and can take several minutes to return.
> `Status: Succeeded` means Azure handed the mail off successfully — it does not
> prove the message survived your spam filter. Confirm it in the inbox.

This proves the address is right, the receiver is enabled, and the delivery path
works — every part of the guardrail except the threshold arithmetic, which is
Azure's to get right and cannot be exercised at $0.00.

- [x] Budget exists — `h1b-zero-spend`, $1.00 monthly, 3 thresholds
- [x] Established that threshold alerts cannot fire at $0.00 spend
- [x] Action Group created, attached to all three thresholds
- [x] Test notification sent and reported `Succeeded`
- [x] **Test email confirmed received, 2026-08-14** — checked in the inbox, not
      inferred from Azure's `Succeeded` status
- [ ] §3 spend check run at the end of each phase — the real day-to-day guardrail

The Action Group is defined in `infra/monitoring.bicep`, not created by hand.
That matters because Step 12's teardown is `az group delete -n rg-h1b`, which
destroys everything in the resource group. The budget is subscription-scoped and
survives, but it references the Action Group by resource ID — a hand-created one
would leave the budget pointing at a dangling ID after any teardown, silently
losing the notification path that was just verified. Recreating the environment
now recreates the alerting with it.

**The operative guardrail is the §3 spend check, run manually after every
deployment.** The budget will alert if spend ever appears, and the delivery path
is now proven, but nothing fires while the design is working.

---

## 3. How to check spend

### The command the plan specifies does not work here

Plan Step 2 says to document "the exact `az consumption usage list` command."
**That command fails on this subscription**, in both forms, tested 2026-08-14:

```
az consumption usage list --top 50
  → 400  Billing Period is not supported in (2023-05-01) API Version for
         Subscription Scope With Web Direct Offer

az consumption usage list --start-date … --end-date …
  → 404  Given subscription … doesn't have valid WebDirect/AIRS offer type
```

The `az consumption` command family is in preview and targets an older billing
offer type. Do not put it in the runbook as though it works. Use one of the two
below instead.

### Quickest check — the budget's own current spend

One call, no rate limits, and it reads the same number the alerts fire on:

```bash
az rest --method get --url "https://management.azure.com/subscriptions/54d2e1cd-805a-4c5e-ac6f-25932378fcd3/providers/Microsoft.Consumption/budgets/h1b-zero-spend?api-version=2021-10-01" --query "properties.currentSpend"
```

**Must read `0.0`.** Anything else means a billable resource exists — find which
and why before continuing. As of 2026-08-14 it reads `0.0`.

### Full breakdown — Cost Management query API

```bash
az rest --method post --url "https://management.azure.com/subscriptions/54d2e1cd-805a-4c5e-ac6f-25932378fcd3/providers/Microsoft.CostManagement/query?api-version=2023-03-01" --headers "Content-Type=application/json" --body '{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"None","aggregation":{"totalCost":{"name":"Cost","function":"Sum"}}}}'
```

> This endpoint rate-limits hard and returns `429 Too many requests` if called
> more than a few times in a row. Back off rather than retrying in a loop.

### Portal

Cost Management + Billing → Cost analysis, scoped to the subscription. Group by
*Resource* to see which resource is responsible. This is the most reliable of
the three and the only one that shows a per-resource breakdown without more API
work.

### The three ways this becomes non-zero

In likelihood order, per plan §7:

1. **Creating an Azure Container Registry out of habit** — ~$5/month, no free
   tier on Basic. Use GHCR instead. This is assumption B8 and the single most
   likely way this project starts costing money.
2. **Setting `minReplicas: 1`** on the web app to dodge cold starts — turns idle
   into a billable state. Accept the 20–30 second cold start.
3. **Blob Storage after month 12** — sources disagree on whether the 5 GB free
   allowance is permanent or 12-month. Assume 12-month. At 175 MB the worst case
   is under $0.01/month, but set a calendar reminder for month 11.

---

## 4. Redeploy

### Infrastructure

All Azure resources are declared in `infra/`. Deployment is declarative and
idempotent — running it against unchanged state is a no-op, verified below.

Preview first. This shows what would change without changing anything:

```bash
az deployment group what-if -g rg-h1b --template-file infra/main.bicep --parameters infra/main.parameters.json --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

Then apply:

```bash
az deployment group create -g rg-h1b --template-file infra/main.bicep --parameters infra/main.parameters.json --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

`--subscription` is passed explicitly on purpose — see §1's warning about two
subscriptions sharing a name.

> **A clean `what-if` must read `N no change`.** If it reports modifications on
> an unchanged template, the template is under-declaring properties that Azure
> populates by default, not detecting real drift. Both container resources and
> `blobServices/default` needed `defaultEncryptionScope`,
> `denyEncryptionScopeOverride`, and `deleteRetentionPolicy` declared for this
> reason. Fix the template rather than learning to ignore the diff — Step 11
> runs these deploys in CI, where a permanently dirty diff hides real changes.

### Step 3 — storage, DONE 2026-08-14

```
STORAGE_ACCOUNT = sth1bhutymqa65yoty
BLOB_ENDPOINT   = https://sth1bhutymqa65yoty.blob.core.windows.net/
CONTAINERS      = raw, curated
LIFECYCLE       = delete blobs under raw/ after 90 days
```

The account name is `st` + prefix + `uniqueString(resourceGroup().id)`. Storage
account names are globally unique, 3–24 characters, **lowercase alphanumeric
only** — which is why the `h1b-` hyphen used for `h1b-web` and `h1b-etl` cannot
appear here. `uniqueString` is deterministic per resource group, so a redeploy
reuses the same account instead of orphaning the old one.

`allowSharedKeyAccess` is `false`, so every data-plane command needs
`--auth-mode login`:

```bash
az storage container list --account-name sth1bhutymqa65yoty --auth-mode login --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3 -o table
```

- [x] Both containers exist
- [x] Redeploy is idempotent — `what-if` reports `5 no change`
- [x] Spend after deployment still `$0.00`

### Step 4 — Azure SQL on the free offer, DONE 2026-08-14

```
SQL_SERVER   = sql-h1b-hutymqa65yoty.database.windows.net
DATABASE     = sqldb-h1b
SKU          = GP_S_Gen5_2 (General Purpose, serverless, 2 vCore max)
LOCATION     = canadacentral
```

Verified with `az sql db show` — every one of these is a value where the wrong
setting costs money:

| Property | Value | Why it matters |
|---|---|---|
| `useFreeLimit` | `true` | opts into the free vCore/storage grant |
| `freeLimitExhaustionBehavior` | `AutoPause` | pauses when the grant runs out. `BillOverUsage` would charge instead |
| `autoPauseDelay` | `60` | minutes idle before pausing |
| `minCapacity` | `0.5` | smallest serverless floor |
| `maxSizeBytes` | `34359738368` | 32 GiB, the free storage allowance |
| `currentBackupStorageRedundancy` | `Local` | geo-redundant backup is billable |
| `azureAdOnlyAuthentication` | `true` | SQL password auth is impossible, not merely discouraged |

#### Connecting

`sqlcmd` (go-sqlcmd, `brew install sqlcmd`) reuses the existing `az login`
session, so no password is involved anywhere:

```bash
sqlcmd -S sql-h1b-hutymqa65yoty.database.windows.net -d sqldb-h1b --authentication-method ActiveDirectoryAzCli -Q "SELECT DB_NAME();"
```

#### Firewall — and why your IP is not in this repo

Two rules exist. `AllowAllWindowsAzureIps` (0.0.0.0) is in the template.
`ClientDevelopmentMachine` is **not** — `clientIpAddress` defaults to empty and
the rule is conditional, because a home IP address does not belong in a public
repository. Pass it at deploy time:

```bash
az deployment group create -g rg-h1b --template-file infra/main.bicep --parameters infra/main.parameters.json --parameters clientIpAddress=<your-ip> --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

To find your IP without handing it to a third-party lookup service, just attempt
the connection — Azure's rejection names the blocked address:

```
Client with IP address 'x.x.x.x' is not allowed to access the server.
```

Re-run the deploy with that value. Changes take up to five minutes to apply.
A changed home IP is the usual cause of a connection that worked yesterday.

> **The rule is never removed by the template.** ARM incremental deployments do
> not delete resources absent from a template, so the conditional only ever
> creates. Deploying later *without* `clientIpAddress` leaves the rule in place
> and `what-if` reports "no change" without mentioning it — verified 2026-08-14.
>
> Two consequences. A mistyped address is permanent: the parameter is an
> unvalidated string, `not-an-ip` passes `az deployment group validate`, and a
> well-formed wrong address silently admits somebody else's machine. And because
> ISPs reassign residential addresses, a stale rule eventually grants network
> reach to a stranger — Entra-only auth still blocks login, but the opening is
> pointless.
>
> Removal is manual, and belongs in the teardown checklist:
>
> ```bash
> az sql server firewall-rule delete -g rg-h1b -s sql-h1b-hutymqa65yoty -n ClientDevelopmentMachine --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
> ```

#### Compatibility level

The one Step 4 check ARM cannot answer — compatibility level is not exposed on
the database resource, only through a real SQL connection:

```bash
sqlcmd -S sql-h1b-hutymqa65yoty.database.windows.net -d sqldb-h1b --authentication-method ActiveDirectoryAzCli -Q "SET NOCOUNT ON; SELECT name, compatibility_level FROM sys.databases WHERE name = DB_NAME();"
```

Returned **170**, comfortably above the 160 floor. Below 160, `salary_trend`'s
`WINDOW` clause is rejected outright and the failure at Step 8 reads like a
syntax error in the SQL rather than a database setting.

- [x] Database exists and reports `Online`
- [x] `useFreeLimit: true` and `freeLimitExhaustionBehavior: AutoPause`
- [x] Entra-only authentication confirmed `true`
- [x] Connected over Entra auth with no password
- [x] Compatibility level 170 ≥ 160
- [x] Spend after deployment still `$0.00`

### Step 5 — Container Apps, DONE 2026-08-14

```
ENVIRONMENT = h1b-env            (Consumption-only, no VNet)
WEB APP     = h1b-web            https://h1b-web.calmwave-8f560d92.canadacentral.azurecontainerapps.io
ETL JOB     = h1b-etl            Manual trigger, replicaTimeout 3600
IMAGE       = mcr.microsoft.com/k8se/quickstart:latest  (placeholder; Step 9 replaces)
```

Managed identity principal IDs — **Step 6 needs both**:

```
h1b-web  b16f09c9-0791-487c-8801-baa35d3435bd   (SystemAssigned, gets db_datareader)
h1b-etl  7cb7a8f0-0417-402c-8971-ee3ca66137a2   (SystemAssigned, gets db_datawriter + blob contributor)
```

> These change if the app or job is deleted and recreated. Re-read them with
> `az containerapp show -n h1b-web -g rg-h1b --query identity.principalId -o tsv`
> before running Step 6's grants.
>
> These are **object IDs**, which is what Azure RBAC wants. Azure SQL wants the
> **application ID** instead — a different GUID for the same identity. Both are
> tabulated in Step 6 below; do not use one where the other belongs.

#### `minReplicas: 0` is the whole cost story — and it demonstrably works

Configured `minReplicas 0`, `maxReplicas 1`, 0.5 CPU / 1 Gi. Idle charges begin
the moment minimum replicas exceeds zero, so this is the single most expensive
value in `infra/containerapps.bicep`. Plan §7 lists raising it as the second most
likely way this project starts costing money.

**Verified by observation, not by reading the setting.** After a no-traffic
window the app reports:

```
Replicas    State
----------  ------------
0           ScaledToZero
```

```bash
az containerapp revision list -n h1b-web -g rg-h1b --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3 --query "[?properties.active].{replicas:properties.replicas, state:properties.runningState}" -o table
```

Checking this properly matters, and it is easy to get wrong. Control-plane `az`
calls do **not** wake the app, but any `curl` to the FQDN does, and the scale
block's `cooldownPeriod` is 300 s — so a check within five minutes of the last
request will show a replica still running and prove nothing. Wait out the full
cooldown with no HTTP requests before drawing a conclusion. An earlier review of
this project concluded the app "never scales to zero" by sampling too soon and
by reading a replica that something else had woken; the correct reading is above.

The real consequence of scale-to-zero is a cold start on the first request after
idle — expected, not a bug.

**Measured 2026-08-14, from a confirmed `ScaledToZero` state:**

```
cold  HTTP 200 in 24.39 s   (0 replicas -> serving)
warm  HTTP 200 in  0.06 s   (immediately after)
```

That sits inside plan §8's predicted 20–30 s, so the prediction holds for the
placeholder image. Expect the real Streamlit image to be slower — it has a
heavier runtime to start — so re-measure after Step 10 rather than reusing this
number.

**Put the figure next to the Azure link**, per §8. A reviewer who waits 25
seconds on a blank page concludes the app is broken; one who was told to expect
it concludes the app scales to zero, which is the point being demonstrated.

#### The environment has no log destination

Deliberate. A Log Analytics workspace is billable past 5 GB of ingestion and
appears nowhere in §7's cost model, so attaching one would breach B3. Live
streaming still works and needs no workspace:

```bash
az containerapp logs show -n h1b-web -g rg-h1b --follow --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3
```

What is lost is queryable history — you cannot ask why a container died an hour
ago. If Step 9's ODBC install proves hard to debug (plan §8 rates that risk
medium-high), adding a workspace is a small reversible change.

#### Deviation: three required image/port parameters, no defaults

Step 5 specifies `targetPort: 8501` **and** the quickstart placeholder image.
Those contradict each other — the quickstart image serves on port 80, so with
8501 nothing accepts a connection, the revision sits in `Activating`
indefinitely, and requests to the FQDN hang with no response. Observed exactly
that on the first deploy; **the ARM deployment still reported `Succeeded`.**

The web app and the ETL job also need *separate* images — Step 9 builds from
`Dockerfile.etl`, Step 10 from `Dockerfile.web` — so a single image parameter
could not express the required end state at all.

So there are three parameters, and **none has a default**:

```
webImage       what the Streamlit app runs
webTargetPort  the port webImage listens on — 80 placeholder, 8501 Streamlit
etlImage       what the ETL job runs
```

All three live in `infra/main.parameters.json`. Requiring them is the point: a
missing parameter fails at validation and names itself —

```
ERROR: Missing input parameters: webTargetPort
```

— whereas a plausible default fails *silently at runtime*, which is the failure
mode above. Step 9 and Step 10 change an image and its port together in the
parameters file; neither can be swapped while forgetting the other.

> **Do not trigger the ETL job while `etlImage` is the placeholder.** The
> quickstart image serves HTTP and never exits, so a manual run consumes the
> full `replicaTimeout` — one hour at 1 vCPU, 3,600 vCPU-seconds, 2% of the
> monthly free grant, for nothing.

#### `what-if` does not reach zero changes here, and cannot

It reports `12 no change, 1 to modify`. The single modification is
`properties.runningStatus: "Running"` on `h1b-web`, which is **server-computed
and not declarable** — confirmed by Bicep rejecting it with
`BCP037: The property "runningStatus" is not allowed`. Unlike Steps 3 and 4,
where every phantom diff was fixable by declaring server defaults, this one is
irreducible. Treat `1 to modify` naming only `runningStatus` as the clean
baseline for this resource, and investigate anything else.

- [x] Environment provisioned, Consumption-only, no VNet
- [x] Web app returns **HTTP 200 over HTTPS** at its FQDN — 4,331 bytes of HTML
- [x] `az containerapp job list` shows `h1b-etl`, Manual, timeout 3600
- [x] `minReplicas: 0` confirmed **and scale-to-zero observed** — `0 replicas, ScaledToZero` after the cooldown
- [x] Both managed identities exist with principal IDs recorded above
- [x] Spend after deployment still `$0.00`

### Step 6 — Identity grants, DONE 2026-08-17

Two grant paths that work nothing like each other:

| | Where it lives | Redeployed by |
|---|---|---|
| Blob access for `h1b-etl` | `infra/roles.bicep` | `az deployment group create` |
| Database users for both | `sql/grant_identities.sql` | **a human, by hand** |

#### The SQL half is manual, and this is the part that gets forgotten

Database users live inside the database, not in ARM. Nothing in Bicep creates
them, and nothing in Bicep notices they are missing.

**Re-run `sql/grant_identities.sql` after a database recreate — and after
recreating the Container App or the ETL job.** Two different breakages, one
symptom (`Login failed for user '<token-identified principal>'`):

| What you recreated | What happens to the users | Why it is easy to miss |
|---|---|---|
| the database | they are dropped with it | `what-if` reports `no change`; ARM never knew they existed |
| the app or the job | they survive, pointing at a dead identity | the name is unchanged — only the application ID behind it moved |

```bash
sqlcmd -b -S sql-h1b-hutymqa65yoty.database.windows.net -d sqldb-h1b --authentication-method ActiveDirectoryAzCli -i sql/grant_identities.sql
```

`ActiveDirectoryAzCli` reuses the `az login` token, so it needs no password —
which is the point, since the server is `azureADOnlyAuthentication` and no
password exists.

**The `-b` is not optional.** Without it `sqlcmd` prints errors and still exits
`0`. Verified: a script whose `CREATE USER` failed and whose every subsequent
grant failed with it exited `0` and was, to the shell, indistinguishable from a
clean run. With `-b` the same script exits `1`.

The script **converges rather than skipping work** — it drops and recreates both
users on every run, so it ends in the correct state regardless of the state it
started in, and the whole thing is one transaction so a failed run changes
nothing. It ends in assertions that `THROW`, not in output for a human to read.

Verified end to end:

```
clean run                              exit 0
h1b-etl corrupted with a stale sid ->  sid repaired to 0xEA8B9706...  exit 0
REFERENCES revoked, assertions run ->  exit 1
h1b-web given db_datawriter        ->  "h1b-web HAS WRITE ACCESS — it must be read-only"
re-run to repair                       exit 0, assertions pass
```

The earlier version of this script guarded with
`IF NOT EXISTS (... WHERE name = 'h1b-etl')` and was subtly useless: a name check
cannot see a stale sid. In the recreated-app case it found the name, skipped, and
left the broken user in place while reporting success — so the recovery procedure
documented here would have done nothing at all.

What each identity got, and deliberately did not:

```
h1b-etl   db_datareader, db_datawriter
          CREATE TABLE, CREATE VIEW
          ALTER ON SCHEMA::dbo, REFERENCES ON SCHEMA::dbo
h1b-web   db_datareader                       <- read-only, nothing else
```

Read-only for the dashboard was **verified by impersonation, not assumed** from
the role name:

```
h1b-web SELECT : OK (expected)
h1b-web INSERT : DENIED -> The INSERT permission was denied on the object ...
h1b-web CREATE : DENIED -> CREATE TABLE permission denied in database ...
```

#### Deviation: the plan's grants do not let the ETL create the schema

Plan Step 6 lists `GRANT CREATE TABLE TO [h1b-etl]` and nothing else. That is
necessary but not sufficient, and both gaps were found by impersonating the user
rather than by reading documentation:

1. **`ALTER ON SCHEMA::dbo`.** Creating a table needs CREATE TABLE on the
   database *and* ALTER on the schema it lands in. Without it Step 8 fails on its
   first statement with `The specified schema name "dbo" either does not exist or
   you do not have permission to use it` — which is a misleading way to report a
   missing permission, since `dbo` obviously exists.
2. **`REFERENCES ON SCHEMA::dbo`.** ALTER does not imply it. Objects created in
   `dbo` are owned by the schema, not by the identity that created them, so
   `h1b-etl` creates `employers` and is then refused permission to point a
   foreign key at it — `The REFERENCES permission was denied on the object`, on a
   table it had created one statement earlier. `filings` declares five such keys.

With both added, an impersonated `h1b-etl` successfully ran CREATE TABLE, CREATE
TABLE with a foreign key, CREATE INDEX with an INCLUDE list, CREATE VIEW, INSERT,
and SELECT through the view. All of it inside a rolled-back transaction — the
database is still empty, `sys.tables` returns nothing, and Step 8 remains the
step that creates the schema.

#### The sid in SQL is the application ID, not the principal ID

A managed identity has two GUIDs and this step touches both:

```
                object / principal ID                 application (client) ID
h1b-web   b16f09c9-0791-487c-8801-baa35d3435bd   b497d91d-7081-4861-9ffb-22d868f31b45
h1b-etl   7cb7a8f0-0417-402c-8971-ee3ca66137a2   06978bea-22bb-416b-8ce6-3cd1c212e0f0
```

**Azure RBAC uses the object ID; Azure SQL uses the application ID.** So
`infra/roles.bicep` assigns to `7cb7a8f0…` while `sys.database_principals` stores
the sid `0xEA8B9706BB226B418CE63CD1C212E0F0`, which is `06978bea…` byte-swapped.

`CREATE USER … FROM EXTERNAL PROVIDER` gets this right on its own — it worked
here without the server needing a managed identity of its own. The trap is only
in the explicit `WITH SID` fallback: a user created from the *object* ID is
created successfully, looks correct in `sys.database_principals`, and can never
log in, because the token the identity presents carries the application ID.

Re-read both, for either identity, with:

```bash
az ad sp list --display-name h1b-etl --query "[].{objectId:id, appId:appId}" -o table
```

#### `what-if` cannot see the role assignment

It reports `1 unsupported` alongside the usual `1 to modify` for
`runningStatus`:

```
(Unsupported) Changes to the resource ... cannot be analyzed because its
resource ID ... cannot be calculated until the deployment is under way.
```

Expected, not a defect. The assignment's name is `guid()` of a `reference()` to
the ETL job's principal ID, which does not exist until deployment runs. **So
`what-if` is not evidence about this resource** — check it directly instead:

```bash
az role assignment list --scope /subscriptions/54d2e1cd-805a-4c5e-ac6f-25932378fcd3/resourceGroups/rg-h1b/providers/Microsoft.Storage/storageAccounts/sth1bhutymqa65yoty --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3 -o table
```

Deployed twice; still exactly one assignment, because the name is derived from
(scope, principal, role) and is therefore stable.

**Recreating the ETL job orphans the old assignment.** A new principal ID means a
new assignment name, so the next deployment creates a second one and leaves the
first — ARM incremental never deletes what a template stopped mentioning, and
Azure does not reap assignments whose principal is gone. Nothing is over-granted,
since principal IDs are never reused, but they accumulate and a security review
will ask about them. Not observed here (confirming it means deleting the job);
reasoned from ARM's documented incremental-mode behaviour. Check and clean up:

```bash
az role assignment list --scope /subscriptions/54d2e1cd-805a-4c5e-ac6f-25932378fcd3/resourceGroups/rg-h1b/providers/Microsoft.Storage/storageAccounts/sth1bhutymqa65yoty --subscription 54d2e1cd-805a-4c5e-ac6f-25932378fcd3 --query "[].{principal:principalId, role:roleDefinitionName, name:name}" -o table
```

Any row whose `principal` is not the current `h1b-etl` principal ID is an orphan;
remove it with `az role assignment delete --ids <the assignment id>`.

#### The database was paused, and that is working as designed

The first connection failed outright:

```
Database 'sqldb-h1b' ... is not currently available. Please retry the connection later.
```

`az sql db show` reported `status: Resuming` and a `pausedDate` three days
earlier. It was `Online` on the next check. This is `autoPauseDelay: 60` doing
its job and is the mechanism that keeps idle cost at zero — not an error to
debug. Retry after waiting. The exact resume duration was not measured here;
plan §8 budgets ~60 seconds.

> **Carry this into Step 9.** The ETL job has `replicaRetryLimit: 1`. If it
> triggers against a paused database the first connection fails outright, and
> whether the single retry lands after the resume finishes is unknown — untested,
> because the resume was not timed. If it does not, the job fails for reasons
> that have nothing to do with the load. Either widen the retry budget or open a
> warm-up connection before the real work, and decide that at Step 9 rather than
> discovering it there.

#### What this step does NOT cover

**Step 7 will hit a wall.** Uploading raw data to Blob needs
`Storage Blob Data Contributor` for *you*, and you do not have it. Subscription
Owner is control-plane only — it lets you delete the storage account but not read
a blob in it — and `allowSharedKeyAccess: false` removes the account-key
fallback. The symptom is `AuthorizationPermissionMismatch`. It is deliberately
not in `roles.bicep`, which grants managed identities, not people:

```bash
az role assignment create --assignee 8ff2eb8b-1fe8-4bb1-9e8f-1a434ee951a8 --role "Storage Blob Data Contributor" --scope /subscriptions/54d2e1cd-805a-4c5e-ac6f-25932378fcd3/resourceGroups/rg-h1b/providers/Microsoft.Storage/storageAccounts/sth1bhutymqa65yoty
```

Neither identity's SQL access has been proven from the workload itself. The
grants are verified by impersonation from an admin session; an actual
managed-identity login is first exercised at Step 9, when `h1b-etl` runs a real
image. Impersonation tests the permissions, not the token path.

- [x] `SELECT name, type_desc FROM sys.database_principals WHERE type = 'E'` returns both identities
- [x] Roles and permissions confirmed per identity; `h1b-web` write attempts denied
- [x] `Storage Blob Data Contributor` on the storage account for `h1b-etl` only
- [x] Role assignment idempotent — deployed twice, one assignment
- [x] Grant script converges — repaired a deliberately corrupted sid, and restored `h1b-web` to read-only after it was given write access
- [x] Grant script fails loudly — assertions `THROW`, and `sqlcmd -b` exits non-zero on drift
- [x] Runbook records that the SQL half is manual and must be repeated after a database recreate
- [x] Spend after deployment still `$0.00`

### Data

<!-- Step 7 onward. Not written yet. -->

`<TO BE WRITTEN>`

---

## 5. Known failure modes

<!-- Filled in as they are actually hit. Seeded from plan §8 with the two that
     are predicted to bite hardest; do not add speculative entries. -->

### `Login failed for user '<token-identified principal>'`

The managed identity has not been granted access to Azure SQL. The Step 6 grants
are manual T-SQL and are easy to forget after recreating the database. Re-run
`sql/grant_identities.sql` as the Entra admin.

### `The specified schema name "dbo" either does not exist or you do not have permission to use it`

Hit while probing Step 6's grants. The schema does exist; the message means the
identity lacks `ALTER ON SCHEMA::dbo`, which creating a table requires in
addition to `CREATE TABLE`. Its sibling is `The REFERENCES permission was denied
on the object …` when creating a foreign key, which needs
`REFERENCES ON SCHEMA::dbo`. Both grants are in `sql/grant_identities.sql`; if
you see either, the script has not been run against this database.

### `Database 'sqldb-h1b' ... is not currently available`

Not a fault. The database is serverless with `autoPauseDelay: 60`, so it pauses
after an hour idle and the next connection fails while it resumes. Check with
`az sql db show -n sqldb-h1b -s sql-h1b-hutymqa65yoty -g rg-h1b --query status`;
`Resuming` means wait and retry. This is the behaviour that keeps idle cost at
zero — see Step 6.

### Titles load rejects rows / `titles` count is not 123,990

The `BIN2` collation was dropped from the DDL. 9,286 of 123,990 titles are
duplicates under Azure SQL's default comparison — 6,599 by case, 2,687 more by
trailing space — and the `UNIQUE` constraint rejects them. The fix belongs in the
DDL before the first load, not in a retry. See plan §6.

---

## 6. Teardown

<!-- Step 12. The one command that removes everything. Not verified yet — do not
     write it here until it has actually been run. -->

`<TO BE WRITTEN>`
