# Prevailing on Azure — Migration Plan (Phase 2)

**Status:** ready to build, but **blocked on Phase 1**
**Depends on:** `docs/plans/h1b-salary-explorer.md` Steps 1–10 complete and deployed
**Author:** architect agent
**Date:** 2026-08-05
**Estimated effort:** 8–12 evenings (12 steps)
**Target cost:** $0.00/month

---

## 1. Goal

Take the working SQLite + Streamlit version of Prevailing and rebuild it on an Azure-native data stack: raw DOL files land in Blob Storage, a containerized Python job transforms and loads them into Azure SQL Database, and the Streamlit dashboard runs in Azure Container Apps — all defined in Bicep, all deployed by GitHub Actions on push, and all inside Azure's permanent free tiers.

The point is that "Azure" stops being a line on a résumé and becomes a thing that demonstrably runs. A reviewer clicks one link and sees a live app; a reviewer who opens the repo sees infrastructure-as-code, passwordless authentication, and a CI/CD pipeline. That second reviewer is the one who makes the hiring decision.

The secondary goal is the migration story itself. Porting SQLite to T-SQL forces real dialect work — `PERCENTILE_CONT`, `IDENTITY`, `NVARCHAR` sizing, bulk loading — and being able to explain *why* the queries changed is a stronger interview answer than having written them correctly the first time.

---

## 2. Assumptions

| # | Assumption | Why |
|---|---|---|
| B1 | Phase 1 is finished and deployed to Streamlit Cloud before this starts | This plan migrates working code; it does not write the pipeline |
| B2 | New Azure account, created with a credit card, no free credits consumed yet | Stated by user |
| B3 | Hard $0 budget — no step may create a billable resource | Stated by user |
| B4 | Repo is `github.com/JustinRheyDavid/prevailing`, public | Public repo = free GitHub Actions minutes and free GHCR image hosting |
| B5 | Region is a single region, `eastus` or `westus2` | Multi-region is meaningless here and doubles cost risk |
| B6 | The Streamlit Cloud deploy stays live during and after migration | Free insurance — if Azure breaks, the demo link still works |
| B7 | Data refresh stays manual (trigger the job by hand) | Scheduled refresh is Step 12's optional extra, not core |
| B8 | Container images are hosted on GitHub Container Registry, not Azure Container Registry | **ACR has no free tier — Basic is ~$5/month.** This is the single most common way this project would start costing money |
| B9 | Azure SQL is configured with "auto-pause until next month" on hitting free limits | The only setting that makes $0 a guarantee rather than a hope |

---

## 3. Out of scope

- **Azure Data Factory** — no meaningful free tier; pipeline runs alone would breach B3
- **Azure Synapse / Databricks / Fabric** — vastly oversized for 1.8M rows, and none are free
- **Azure Key Vault** — managed identity removes the need for stored secrets entirely; adding Key Vault would be cargo-culting
- **VNet integration, private endpoints, Application Gateway** — each carries a real hourly charge and protects nothing here
- **Custom domain and TLS certificate** — Container Apps gives a free `*.azurecontainerapps.io` HTTPS URL
- **Azure Monitor alerts beyond the free budget alert** — Log Analytics ingestion is billable past 5 GB
- **Multi-region, geo-replication, high availability** — a portfolio dashboard does not need four nines
- **Removing the SQLite path** — it stays, for local dev and tests

---

## 4. Architecture

```
 GitHub repo (JustinRheyDavid/prevailing)
   │
   ├── push to main ──▶ GitHub Actions ──┬──▶ build image ──▶ ghcr.io (free, public)
   │                    (OIDC, no secrets)│
   │                                      └──▶ az deployment (Bicep) ──▶ Azure
   │
   └─────────────────────────────────────────────────────────────────────┐
                                                                          ▼
  ┌──────────────────────────── Azure Resource Group: rg-prevailing ────────────────────────────┐
  │                                                                                              │
  │   Blob Storage                 Container Apps Job              Azure SQL Database            │
  │   ┌──────────────┐   read      ┌──────────────────┐   write    ┌────────────────────┐        │
  │   │ raw/  *.xlsx │ ──────────▶ │  prevailing-etl  │ ─────────▶ │  sqldb-prevailing  │        │
  │   │ curated/*.pq │ ◀────────── │  (manual trigger)│            │  serverless, free  │        │
  │   └──────────────┘   write     └──────────────────┘            │  auto-pause        │        │
  │                                         │                       └────────────────────┘        │
  │                                  managed identity                        ▲                    │
  │                                                                          │ read               │
  │                                            Container App                 │                    │
  │                                            ┌──────────────────┐          │                    │
  │                                            │ prevailing-web   │ ─────────┘                    │
  │                                            │ Streamlit,       │                               │
  │                                            │ min replicas = 0 │                               │
  │                                            └──────────────────┘                               │
  │                                                     │ HTTPS                                   │
  └─────────────────────────────────────────────────────┼───────────────────────────────────────┘
                                                        ▼
                                    https://prevailing-web.<region>.azurecontainerapps.io
```

### Component ownership

| Component | Owns | Does not |
|---|---|---|
| `infra/*.bicep` | Every Azure resource, its SKU, and its identity grants | Contain application logic or data |
| `src/db/` | Backend selection and both SQL dialects behind one interface | Know anything about Streamlit |
| `src/etl/` | Reading blobs, transforming, bulk-loading to Azure SQL | Create infrastructure |
| `Dockerfile.web` | The Streamlit runtime image | Run the ETL |
| `Dockerfile.etl` | The ETL runtime image, including ODBC driver | Serve HTTP |
| `.github/workflows/` | Build, test, push image, deploy infra | Hold long-lived credentials |

### File tree — additions to the Phase 1 repo

```
prevailing/
├── infra/
│   ├── main.bicep                 # resource group scope, wires the modules
│   ├── storage.bicep              # storage account + containers
│   ├── sql.bicep                  # SQL server + free-tier database
│   ├── containerapps.bicep        # environment + web app + etl job
│   └── main.parameters.json       # region, names, admin AAD object id
├── src/
│   ├── db/
│   │   ├── __init__.py            # get_backend() factory, reads DB_BACKEND
│   │   ├── base.py                # the five query signatures, as a Protocol
│   │   ├── sqlite_impl.py         # Phase 1 queries, moved here unchanged
│   │   └── azure_impl.py          # T-SQL versions of the same five
│   └── etl/
│       ├── blob.py                # download raw / upload curated
│       └── load_azure.py          # bulk load Parquet into Azure SQL
├── sql/
│   └── schema_azure.sql           # T-SQL DDL, see §6
├── Dockerfile.web
├── Dockerfile.etl
├── .github/workflows/
│   ├── ci.yml                     # pytest on every PR
│   └── deploy.yml                 # build + push + bicep deploy on main
└── docs/
    └── azure-runbook.md           # how to redeploy, how to check spend
```

**Design notes:**

- **Container Apps over App Service** — App Service's free F1 tier is 32-bit only with a 60 CPU-minute daily cap; pandas and Streamlit will not fit comfortably. Container Apps scales to zero and gives 180,000 vCPU-seconds free per month, which is far more headroom.
- **GitHub Container Registry over ACR** — see B8. GHCR is free for public images and Container Apps pulls from it without special configuration.
- **Managed identity over connection strings** — the Container App authenticates to Azure SQL as itself. No password exists anywhere, so no password can leak from the repo. This is also the single most "senior" thing in the project.
- **OIDC federated credentials over a service principal secret** — GitHub Actions gets a short-lived token per run. No `AZURE_CREDENTIALS` secret to rotate or leak.
- **Container Apps Job over an Azure Function** — the ETL is a batch process that runs for minutes and needs pandas plus an ODBC driver. That is a container-shaped problem, not a function-shaped one.
- **Both SQL dialects kept behind one interface** — lets `pytest` run against SQLite in CI with no cloud dependency, while production uses Azure SQL.

---

## 5. Build steps

Twelve steps in five phases. **Do not start Phase B until Step 2's budget alert is confirmed working** — that guardrail is what makes the rest safe to experiment with.

---

## Phase A — Account and guardrails

### Step 1 — Create the Azure account
**Files:** `docs/azure-runbook.md` (new, stub)

Create a free Azure account. Record the subscription ID and tenant ID in the runbook. Create resource group `rg-prevailing` in the chosen region.

Note honestly in the runbook: a credit card is required at signup, and the account starts with trial credit that expires. Nothing in this plan depends on that credit.

**Done when:** `az account show` returns the subscription, and `az group show -n rg-prevailing` returns the group.

---

### Step 2 — Spend guardrails, before any resource exists
**Files:** `docs/azure-runbook.md`

Non-negotiable, and it comes before infrastructure on purpose:

1. Create a budget on the subscription with a **$1.00** monthly amount and alerts at 50%, 80%, and 100% to your email.
2. Verify the alert email arrives — send a test or set the threshold to a value already exceeded, then reset it.
3. Write the runbook's "how to check spend" section: the exact `az consumption usage list` command and the portal path to Cost Analysis.

A $1 budget on a project designed to cost $0 means any alert at all is a real signal, not noise.

**Done when:** the budget exists, an alert email has actually landed in your inbox, and the runbook documents both.

---

## Phase B — Infrastructure as code

### Step 3 — Storage account in Bicep
**Files:** `infra/main.bicep`, `infra/storage.bicep`, `infra/main.parameters.json`

Standard LRS, Hot tier, StorageV2, hierarchical namespace **off** (ADLS Gen2 features are not needed and complicate SDK access). Two containers: `raw` and `curated`. Add a lifecycle management rule deleting blobs in `raw/` after 90 days.

Deploy with `az deployment group create`.

**Done when:** `az storage container list` shows both containers, and re-running the deployment is idempotent — second run reports no changes.

---

### Step 4 — Azure SQL Database on the free offer
**Files:** `infra/sql.bicep`

The critical resource. Configuration that must be exact:

- SKU: `GP_S_Gen5_2` (General Purpose, serverless, 2 vCore max)
- `useFreeLimit: true`
- `freeLimitExhaustionBehavior: 'AutoPause'` — **not** `BillOverUsage`
- `autoPauseDelay: 60` (minutes)
- `minCapacity: 0.5`
- `maxSizeBytes`: 32 GB
- Entra ID (Azure AD) admin set to your user; **SQL authentication disabled entirely**
- Firewall rule: `AllowAzureServices` (0.0.0.0) plus your current home IP for development

Disabling SQL auth is deliberate — it makes password-based access impossible rather than merely discouraged.

**Done when:** the database exists, `az sql db show` confirms `useFreeLimit: true` and `AutoPause` exhaustion behavior, and you can connect from Azure Data Studio using Entra ID auth.

---

### Step 5 — Container Apps environment, web app, and ETL job
**Files:** `infra/containerapps.bicep`

- Consumption-only environment (no workload profiles, no VNet — both create charges)
- `prevailing-web`: external ingress on port 8501, **`minReplicas: 0`**, `maxReplicas: 1`, system-assigned managed identity, 0.5 vCPU / 1 GiB
- `prevailing-etl`: Container Apps **Job**, manual trigger type, 1 vCPU / 2 GiB, `replicaTimeout: 3600`, system-assigned managed identity
- Both point initially at `mcr.microsoft.com/k8se/quickstart:latest` as a placeholder; Step 9 swaps in the real images

`minReplicas: 0` is what makes idle cost genuinely zero — idle charges only apply when minimum replicas is greater than zero.

**Done when:** the placeholder web app returns a page over HTTPS at its generated FQDN, and `az containerapp job list` shows the ETL job.

---

### Step 6 — Grant the managed identities access
**Files:** `infra/*.bicep`, `sql/grant_identities.sql`

Two grant paths, and they work differently:

- **Storage:** Bicep role assignment. ETL job identity gets `Storage Blob Data Contributor` on the storage account.
- **SQL:** cannot be done in Bicep. Connect as the Entra admin and run T-SQL:

```sql
CREATE USER [prevailing-etl] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datawriter ADD MEMBER [prevailing-etl];
ALTER ROLE db_datareader ADD MEMBER [prevailing-etl];
GRANT CREATE TABLE TO [prevailing-etl];

CREATE USER [prevailing-web] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [prevailing-web];
```

The web app gets **read-only**. It has no business writing.

**Done when:** `SELECT name, type_desc FROM sys.database_principals WHERE type = 'E'` returns both identities, and the runbook documents that this step is manual and must be repeated if the database is recreated.

---

## Phase C — Data path

### Step 7 — Upload raw data to Blob
**Files:** `src/etl/blob.py`

Two functions: `upload_raw(local_path)` and `download_raw(blob_name, dest)`, both authenticating via `DefaultAzureCredential` so the same code works with your login locally and the managed identity in Azure.

Upload the FY2024/2025/2026 files. **Upload the Parquet conversions from Phase 1, not the raw `.xlsx`** — Parquet is roughly 10x smaller, which keeps storage inside the free 5 GB and makes the ETL job dramatically faster.

**Done when:** `az storage blob list -c raw` shows the files, total container size is under 1 GB, and `download_raw` round-trips a file locally.

---

### Step 8 — T-SQL schema and the dialect split
**Files:** `sql/schema_azure.sql`, `src/db/base.py`, `src/db/sqlite_impl.py`, `src/db/azure_impl.py`, `src/db/__init__.py`

Move Phase 1's `queries.py` to `src/db/sqlite_impl.py` **unchanged**. Define the five function signatures in `base.py` as a `Protocol`. Write `azure_impl.py` with T-SQL equivalents. `__init__.py` exposes `get_backend()` reading the `DB_BACKEND` environment variable, defaulting to `sqlite`.

Dialect differences that will bite (see §6 for the full list): `LIMIT` becomes `TOP` or `OFFSET/FETCH`, `INTEGER PRIMARY KEY` becomes `INT IDENTITY(1,1)`, `TEXT` becomes sized `NVARCHAR`, and percentiles move out of pandas into native `PERCENTILE_CONT`.

**Done when:** the existing Phase 1 test suite passes unchanged against `DB_BACKEND=sqlite`, and every function in `azure_impl.py` returns identical shaped results against the live Azure SQL database.

---

### Step 9 — ETL job: containerize and load
**Files:** `Dockerfile.etl`, `src/etl/load_azure.py`

Dockerfile must install the Microsoft ODBC driver — `pyodbc` will not connect without it:

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y curl gnupg unixodbc \
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
    > /etc/apt/sources.list.d/mssql.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
 && rm -rf /var/lib/apt/lists/*
```

`load_azure.py` reads Parquet from `curated/`, then bulk inserts with `cursor.fast_executemany = True` and batches of 10,000. Row-by-row inserts of 1.8M rows will take hours and burn the compute grant — this flag is not optional.

Build locally, push to `ghcr.io/justinrheydavid/prevailing-etl:latest`, update the job image, execute it.

**Done when:** `az containerapp job start` completes successfully and `SELECT COUNT(*) FROM filings` in Azure SQL matches the SQLite row count from Phase 1.

---

## Phase D — Application

### Step 10 — Containerize the dashboard
**Files:** `Dockerfile.web`, `app.py` (one-line change)

`app.py` changes only where it opens the database: replace the direct SQLite connection with `get_backend()`. Nothing else in the UI changes — that is the payoff for the Step 8 interface split.

Dockerfile.web needs the same ODBC driver plus `streamlit run app.py --server.port=8501 --server.address=0.0.0.0`. Add a `/_stcore/health` health probe.

Set `DB_BACKEND=azure` and `AZURE_SQL_SERVER` as environment variables on the container app.

**Done when:** the Container Apps FQDN serves the dashboard, a filter interaction returns correct data from Azure SQL, and the app cold-starts from zero replicas in under 30 seconds.

---

## Phase E — Automation and documentation

### Step 11 — GitHub Actions with OIDC
**Files:** `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`

Set up federated credentials: an Entra app registration with a federated credential scoped to `repo:JustinRheyDavid/prevailing:ref:refs/heads/main`, granted Contributor on `rg-prevailing`. Store only `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` as repo variables — **no client secret exists**.

- `ci.yml`: on pull request — `pytest` against SQLite, plus `ruff`
- `deploy.yml`: on push to `main` — `azure/login@v2` with OIDC, build both images, push to GHCR, `az deployment group create`, then `az containerapp update` to roll the new image

**Done when:** a trivial commit to `main` results in a new revision serving automatically, and the Actions log shows an OIDC token exchange with no secret in the environment.

---

### Step 12 — Runbook, README, and teardown
**Files:** `docs/azure-runbook.md`, `README.md`

Runbook must cover: redeploy from scratch, refresh data with a new DOL quarter, check current spend, what to do if the budget alert fires, and **how to tear the whole thing down** (`az group delete -n rg-prevailing --yes`).

README gains an "Architecture on Azure" section with the §4 diagram, both live links (Streamlit Cloud and Azure), and a short "Why these services" paragraph naming the free-tier limits — that paragraph is what shows a reviewer you understand cloud cost, which is a thing hiring managers worry about with junior engineers.

Optional if time allows: change the ETL job trigger from `Manual` to `Schedule` with cron `0 6 1 * *` (monthly).

**Done when:** someone else could follow the runbook to deploy the project into their own subscription, and the README's Azure link works from a private browser window.

---

## 6. Data model and interfaces

### T-SQL schema

```sql
CREATE TABLE employers (
    employer_id     INT IDENTITY(1,1) PRIMARY KEY,
    employer_name   NVARCHAR(300) NOT NULL UNIQUE,
    raw_name_sample NVARCHAR(300) NULL
);

CREATE TABLE occupations (
    soc_id    INT IDENTITY(1,1) PRIMARY KEY,
    soc_code  NVARCHAR(10)  NOT NULL UNIQUE,
    soc_title NVARCHAR(200) NOT NULL
);

CREATE TABLE filings (
    filing_id       BIGINT IDENTITY(1,1) PRIMARY KEY,
    case_number     NVARCHAR(50)  NOT NULL UNIQUE,
    employer_id     INT           NOT NULL REFERENCES employers(employer_id),
    soc_id          INT           NOT NULL REFERENCES occupations(soc_id),
    job_title       NVARCHAR(300) NOT NULL,
    worksite_city   NVARCHAR(100) NULL,
    worksite_state  CHAR(2)       NULL,
    annual_wage     DECIMAL(12,2) NULL,
    prevailing_wage DECIMAL(12,2) NULL,
    full_time       BIT           NULL,
    fiscal_year     SMALLINT      NOT NULL,
    case_status     NVARCHAR(30)  NOT NULL,
    is_outlier      BIT           NOT NULL DEFAULT 0
);

CREATE INDEX idx_filings_title ON filings(job_title)
    INCLUDE (annual_wage, fiscal_year, is_outlier);
CREATE INDEX idx_filings_city  ON filings(worksite_city, worksite_state)
    INCLUDE (annual_wage, is_outlier);
CREATE INDEX idx_filings_year  ON filings(fiscal_year);
```

`INCLUDE` columns are the T-SQL improvement worth noting in the README: they let the percentile queries be answered from the index alone, without touching the table.

### Dialect differences to expect

| Concern | SQLite (Phase 1) | Azure SQL (Phase 2) |
|---|---|---|
| Autoincrement | `INTEGER PRIMARY KEY` | `INT IDENTITY(1,1) PRIMARY KEY` |
| Strings | `TEXT`, unbounded | `NVARCHAR(n)`, must size — oversizing wastes index space |
| Money | `REAL` | `DECIMAL(12,2)` — exact, no float drift |
| Booleans | `INTEGER` 0/1 | `BIT` |
| Row limiting | `LIMIT 20` | `SELECT TOP 20` or `OFFSET 0 ROWS FETCH NEXT 20 ONLY` |
| Percentiles | not supported — computed in pandas | `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ...) OVER (PARTITION BY ...)` |
| Parameters | `?` | `?` via pyodbc (same) |
| Bulk insert | `executemany` | `fast_executemany = True`, batches of 10,000 |
| Case sensitivity | case-sensitive `LIKE` | collation-dependent, usually case-**in**sensitive |

That last row is a real behavioral difference, not a nuisance: a title search for `"data analyst"` returns nothing in SQLite but matches in Azure SQL. Test for it.

### Backend interface

```python
# src/db/base.py
class Backend(Protocol):
    def salary_percentiles(self, job_title, city, state, fiscal_year,
                           include_outliers=False) -> DataFrame: ...
    def top_employers(self, job_title, city, limit=20) -> DataFrame: ...
    def salary_by_city(self, job_title, min_filings=10) -> DataFrame: ...
    def salary_trend(self, job_title, city) -> DataFrame: ...
    def title_search(self, prefix, limit=25) -> list[str]: ...

# src/db/__init__.py
def get_backend() -> Backend:
    """DB_BACKEND: 'sqlite' (default) | 'azure'"""
```

### Azure SQL connection, passwordless

```python
from azure.identity import DefaultAzureCredential
import pyodbc, struct

TOKEN_ATTR = 1256  # SQL_COPT_SS_ACCESS_TOKEN

def connect(server: str, database: str) -> pyodbc.Connection:
    token = DefaultAzureCredential().get_token(
        "https://database.windows.net/.default").token.encode("utf-16-le")
    packed = struct.pack("<I", len(token)) + token
    conn_str = (f"Driver={{ODBC Driver 18 for SQL Server}};"
                f"Server={server};Database={database};"
                f"Encrypt=yes;TrustServerCertificate=no;")
    return pyodbc.connect(conn_str, attrs_before={TOKEN_ATTR: packed})
```

Works unchanged locally (your `az login`) and in Azure (managed identity). No branching, no secrets.

---

## 7. Cost model

Every line must read $0.00 or the design is wrong.

| Resource | Free allowance | Expected usage | Cost |
|---|---|---|---|
| Azure SQL Database | 100,000 vCore-sec + 32 GB/month, lifetime | ~3,000 vCore-sec, ~1 GB | $0.00 |
| Container Apps (web) | 180,000 vCPU-sec + 2M requests/month | ~5,000 vCPU-sec at `minReplicas: 0` | $0.00 |
| Container Apps (ETL job) | shares the same grant | ~1,800 vCPU-sec/month | $0.00 |
| Container Apps environment | free when Consumption-only, no VNet | — | $0.00 |
| Blob Storage | 5 GB LRS hot | ~400 MB Parquet | $0.00 first 12 months |
| Bandwidth out | 100 GB/month free | negligible | $0.00 |
| GitHub Container Registry | free for public images | 2 images | $0.00 |
| GitHub Actions | free for public repos | ~200 min/month | $0.00 |
| Entra ID app registration | free | 1 | $0.00 |
| **Total** | | | **$0.00** |

**The three ways this becomes non-zero, in likelihood order:**

1. Creating an Azure Container Registry out of habit — ~$5/month, and Basic ACR has no free tier. Use GHCR. This is B8 and it is the most likely mistake.
2. Setting `minReplicas: 1` on the web app to avoid cold starts — turns idle into a billable state. Accept the 20–30 second cold start instead.
3. Blob Storage after month 12 — sources disagree on whether the 5 GB is permanently free or 12-month only. Assume 12-month. At ~400 MB the worst case is roughly $0.01/month, but set a calendar reminder for month 11 to check.

---

## 8. Risks

| Risk | Likelihood | Mitigation / fallback |
|---|---|---|
| **Accidental spend** | Medium | Step 2's $1 budget alert exists before any resource. Teardown is one command, documented in Step 12. |
| **ODBC driver install fails in the container** | Medium-high | Very common and fiddly. Fallback: `pymssql`, which is pip-installable with no system driver — but it does not support Entra token auth cleanly, so you would fall back to SQL authentication and lose the passwordless story. Try `mcr.microsoft.com/mssql-tools` base images before giving up. |
| **Managed identity to SQL fails** | Medium | The Step 6 grants are manual T-SQL and easy to forget after a DB recreate. Symptom is `Login failed for user '<token-identified principal>'`. Runbook must call this out explicitly. |
| **Free-tier DB auto-pauses mid-demo** | Medium | With `AutoPause` exhaustion behavior the DB pauses if the monthly grant runs out; a 60-second resume delay looks broken to a recruiter. Mitigate by keeping `autoPauseDelay` at 60 min, and put "first load may take ~60s" next to the Azure link. |
| **Cold start makes the demo look slow** | High | Scale-to-zero means the first request wakes a replica. Expected, not a bug. Note it beside the link, and keep the Streamlit Cloud version as the primary demo link if it matters. |
| **Bicep learning curve stalls Phase B** | Medium | Bicep is the least familiar piece here. Fallback: `az cli` scripts in `infra/deploy.sh` — less impressive, still reproducible, and can be converted to Bicep later. |
| **1.8M row load exceeds job timeout** | Low-medium | `replicaTimeout: 3600` gives an hour, ample with `fast_executemany`. Fallback: load one fiscal year per job execution. |
| **Migration never finishes and Phase 1 rots** | Medium — the real project risk | Phase 1 stays deployed and is the résumé link. Azure is strictly additive. If Phase E never happens, nothing is lost. |

---

## 9. Open questions

1. **Which Azure link is the primary demo?** Recommendation: keep Streamlit Cloud as the link on the résumé (always warm, instant), and present Azure as "also deployed on Azure with IaC and CI/CD" with its own link in the README. Best of both.
2. **Region.** `eastus` has the widest service availability and lowest chance of a free-tier capacity error. Any reason to prefer somewhere closer to you?
3. **Is the ETL job worth containerizing at all, versus running the load from GitHub Actions?** Actions would be simpler and equally free. The Container Apps Job is meaningfully more Azure-native — which is what you said you wanted — but it is roughly two extra evenings. Confirm you still want it.
4. **Scheduled refresh — yes or no?** DOL publishes quarterly, so a monthly cron is mostly idle. It is three lines of Bicep and demonstrates orchestration. Recommendation: yes, as Step 12's optional extra.
5. **Do you want a `dev` and `prod` environment split?** Realistic, and doubles both resource count and free-tier consumption. Recommendation: no. One environment, and say so in the README rather than pretending otherwise.

---

## Handoff

This plan is blocked until Phase 1 ships. That ordering is deliberate — Step 8 moves working code between dialects, and there is no working code to move yet.

When it unblocks, the coder should work Phase A → E in order, and must not skip Step 2. If Step 4's free-tier flags cannot be set as written (Azure occasionally gates the free offer per subscription), stop and revise this plan rather than deploying a billable SKU "temporarily".
