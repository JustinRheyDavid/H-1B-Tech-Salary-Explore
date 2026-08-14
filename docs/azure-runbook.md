# Azure runbook — H-1B Tech Salary Explorer

Operational notes for the Azure deployment (Phase 2). How to stand it up, how to
check what it costs, and how to tear it down.

**Status: stub.** Steps 1 and 2 are not done. Every `<FILL IN>` below is a real
blank, not a placeholder for something already known. Do not copy values into
them from anywhere but a live `az` command.

The build plan this follows is [`docs/plans/azure-migration.md`](plans/azure-migration.md).

---

## 0. Settled decisions

Both get baked into a federated credential (Step 11) and a manual T-SQL grant
(Step 6), where changing them is annoying. Decided 2026-08-14, before any
resource existed.

### 0.1 Region — `eastus`

```
REGION = eastus
```

Per plan §9.2: widest service availability and the lowest chance of a free-tier
capacity error, which is a real failure mode for the Azure SQL free offer.
Allowed by assumption B5.

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
- [x] `az group show` returns the group — `rg-h1b`, `eastus`, `Succeeded`
- [x] Subscription ID and tenant ID recorded above
- [x] Pre-flight check passed — `allowedToCreateApps: true`, so Step 11 is viable
- [x] Offer type checked — Pay-As-You-Go, **no spending limit**, see §2.1

---

## 2. Spend guardrails

**Do not start Phase B (Step 3, the first Bicep deploy) until this section is
complete and an alert email has actually arrived.** That ordering is the whole
point — a guardrail added after the resources exist has already failed at the
one job it had.

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

### Verify the alert actually fires — NOT YET DONE

An unverified alert is not a guardrail, and this one has not been verified. The
budget exists and reads `currentSpend: 0.0`, but no alert email has been proven
to arrive. **At $0.00 spend against a $1.00 budget, no threshold is crossed, so
nothing will fire on its own.**

To verify, temporarily lower the budget amount so that current spend exceeds
50% of it, wait for the alert, then reset to $1.00. At exactly $0.00 spend even
that will not fire — so realistically this can only be verified once some real
usage exists, i.e. after the first resource is deployed in Phase B.

**This is a genuine gap in the plan's ordering.** Step 2 says the alert email
must land before Phase B begins, but a $0.00 subscription cannot cross any
threshold to produce one. Resolve it one of two ways:

1. Deploy Step 3's storage account (free, and the least risky billable-capable
   resource), then verify the alert against real usage before Steps 4–6.
2. Accept an unverified alert and rely on the §3 spend check being run manually
   at every step. Weaker, and worth writing down as a conscious choice.

```
ALERT_TEST_METHOD   = <FILL IN>
ALERT_EMAIL_ARRIVED = <FILL IN>     # date/time the email actually landed
```

- [x] Budget exists — `h1b-zero-spend`, $1.00 monthly, 3 thresholds
- [ ] An alert email has landed in the inbox — confirmed, not assumed
- [ ] Threshold reset to 50/80/100 after any test
- [ ] Resolution chosen for the ordering gap above

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

<!-- Step 12. Not written yet — needs Phases B through E to exist first. -->

`<TO BE WRITTEN>`

---

## 5. Known failure modes

<!-- Filled in as they are actually hit. Seeded from plan §8 with the two that
     are predicted to bite hardest; do not add speculative entries. -->

### `Login failed for user '<token-identified principal>'`

The managed identity has not been granted access to Azure SQL. The Step 6 grants
are manual T-SQL and are easy to forget after recreating the database. Re-run
`sql/grant_identities.sql` as the Entra admin.

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
