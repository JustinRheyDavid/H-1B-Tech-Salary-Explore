# H-1B Tech Salary Explorer on Azure — Migration Plan (Phase 2)

**Status:** unblocked — Phase 1 shipped and is deployed
**Depends on:** `docs/plans/h1b-salary-explorer.md` Steps 1–10 — complete
**Author:** architect agent
**Date:** 2026-08-05
**Reconciled against the shipped code:** 2026-08-13
**Estimated effort:** 8–12 evenings (12 steps)
**Target cost:** $0.00/month

> **Reconciliation, 2026-08-13.** This plan was written on 2026-08-05, before
> Phase 1 built anything. Phase 1 then shipped a schema and a query layer
> materially different from what §6 assumed, and this document was not updated
> as it went — the same drift that let Phase 1's own plan mislead four reviews
> with function names nobody had written.
>
> Everything below now matches `src/load.py`, `src/queries.py` and `app.py` as
> they stand at `acb12af`. The corrections are marked inline. The four that
> change what gets built:
>
> 1. **The schema is six tables, not three.** `titles`, `locations` and
>    `visa_classes` are lookups; `filings` holds ids. §6 is rewritten.
> 2. **Azure SQL's default collation breaks the `titles` load.** 9,286 of the
>    123,990 titles are duplicates under its comparison rules — case and
>    trailing spaces both. A default `UNIQUE` collapses them and the load fails
>    on duplicate keys. This is a hard stop at Step 9, and the plan previously
>    had the risk pointing the wrong way.
> 3. **The backend interface is seven functions, not five.** `wage_distribution`
>    and `fiscal_years` were added during Phase 1's Step 8.
> 4. **Percentiles are already computed in SQL, not in pandas.** Phase 1
>    implements `PERCENTILE_CONT`'s exact definition by hand, which makes the
>    two backends checkable row-for-row rather than approximately.
>
> **Second pass, same day.** The reconciliation above was reviewed and did not
> survive it intact. `visa_classes` had been sized `NVARCHAR(10)` against
> 15-character data, which aborts the Step 9 load; the dialect table said
> nothing about T-SQL's `GROUP BY` strictness, which four of the seven queries
> violate; `PERCENTILE_CONT` was presented as a drop-in when it is window-only;
> and the `BIN2` fix in finding 2 silently breaks `title_search`'s `LIKE`. All
> four are corrected below — in §6, except the compatibility-level check, which
> belongs to Step 4. The lesson is the one this document already
> demonstrates twice: a schema written from prose rather than from the data is
> wrong in ways nobody notices until the load stops.

---

## 1. Goal

Take the working SQLite + Streamlit version of the H-1B Tech Salary Explorer and rebuild it on an Azure-native data stack: raw DOL files land in Blob Storage, a containerized Python job transforms and loads them into Azure SQL Database, and the Streamlit dashboard runs in Azure Container Apps — all defined in Bicep, all deployed by GitHub Actions on push, and all inside Azure's permanent free tiers.

The point is that "Azure" stops being a line on a résumé and becomes a thing that demonstrably runs. A reviewer clicks one link and sees a live app; a reviewer who opens the repo sees infrastructure-as-code, passwordless authentication, and a CI/CD pipeline. That second reviewer is the one who makes the hiring decision.

The secondary goal is the migration story itself. Porting SQLite to T-SQL forces real dialect work — `PERCENTILE_CONT`, collation, `NVARCHAR` sizing, bulk loading — and being able to explain *why* the queries changed is a stronger interview answer than having written them correctly the first time.

The collation problem in §6 is the best of these to be able to tell: two
databases, the same data, one `UNIQUE` constraint that means different things
in each, and 9,286 rows that vanish if nobody checks. That is a migration
story, not a syntax exercise.

---

## 2. Assumptions

| # | Assumption | Why |
|---|---|---|
| B1 | ~~Phase 1 is finished and deployed to Streamlit Cloud before this starts~~ **Satisfied.** Phase 1 is merged to `main` and live | This plan migrates working code; it does not write the pipeline |
| B2 | New Azure account, created with a credit card, no free credits consumed yet | Stated by user. **Still to do — Steps 1–2** |
| B3 | Hard $0 budget — no step may create a billable resource | Stated by user |
| B4 | Repo is `github.com/JustinRheyDavid/H-1B-Tech-Salary-Explore`, public | **Corrected** — this plan said `prevailing`, a name never used. It matters: the repo slug is the OIDC federated-credential subject in Step 11 and the GHCR image path in Step 9, and neither error message names the string it rejected |
| B5 | Region is a single region, `eastus` or `westus2` | Multi-region is meaningless here and doubles cost risk |
| B6 | The Streamlit Cloud deploy stays live during and after migration | Free insurance — if Azure breaks, the demo link still works |
| B7 | Data refresh stays manual (trigger the job by hand) | Scheduled refresh is Step 12's optional extra, not core |
| B8 | Container images are hosted on GitHub Container Registry, not Azure Container Registry | **ACR has no free tier — Basic is ~$5/month.** This is the single most common way this project would start costing money |
| B9 | Azure SQL is configured with "auto-pause until next month" on hitting free limits | The only setting that makes $0 a guarantee rather than a hope |

---

## 3. Out of scope

- **Azure Data Factory** — no meaningful free tier; pipeline runs alone would breach B3
- **Azure Synapse / Databricks / Fabric** — vastly oversized for 850,321 rows, and none are free
- **Azure Key Vault** — managed identity removes the need for stored secrets entirely; adding Key Vault would be cargo-culting
- **VNet integration, private endpoints, Application Gateway** — each carries a real hourly charge and protects nothing here
- **Custom domain and TLS certificate** — Container Apps gives a free `*.azurecontainerapps.io` HTTPS URL
- **Azure Monitor alerts beyond the free budget alert** — Log Analytics ingestion is billable past 5 GB
- **Multi-region, geo-replication, high availability** — a portfolio dashboard does not need four nines
- **Removing the SQLite path** — it stays, for local dev and tests

---

## 4. Architecture

Resource names below keep the `prevailing-` prefix from this plan's original
draft. They are Azure resource names, not product names, and nothing reads them
but Bicep — but see §9.6 before committing to them.

```
 GitHub repo (JustinRheyDavid/H-1B-Tech-Salary-Explore)
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
H-1B-Tech-Salary-Explore/
├── infra/
│   ├── main.bicep                 # resource group scope, wires the modules
│   ├── storage.bicep              # storage account + containers
│   ├── sql.bicep                  # SQL server + free-tier database
│   ├── containerapps.bicep        # environment + web app + etl job
│   └── main.parameters.json       # region, names, admin AAD object id
├── src/
│   ├── queries.py                 # UNCHANGED — stays the SQLite implementation
│   ├── db/
│   │   ├── __init__.py            # get_backend() factory, reads DB_BACKEND
│   │   ├── base.py                # the seven query signatures, as a Protocol
│   │   ├── sqlite_impl.py         # thin class delegating to src/queries.py
│   │   └── azure_impl.py          # T-SQL versions of the same seven
│   └── etl/
│       ├── blob.py                # download raw / upload curated
│       └── load_azure.py          # bulk load Parquet into Azure SQL
├── sql/
│   ├── schema_azure.sql           # T-SQL DDL, see §6
│   └── grant_identities.sql       # Step 6's manual grants
├── Dockerfile.web
├── Dockerfile.etl
├── .github/workflows/
│   ├── ci.yml                     # pytest on every PR
│   └── deploy.yml                 # build + push + bicep deploy on main
└── docs/
    └── azure-runbook.md           # how to redeploy, how to check spend
```

> **Corrected.** This tree originally moved `src/queries.py` into
> `src/db/sqlite_impl.py`. Do not. Three things depend on where that file is:
> `app.py` imports `from src import load, queries`, `tests/test_queries.py`
> imports `src.queries`, and the README points a reviewer at it by path as the
> SQL certificate's exhibit. Moving it churns all three to no benefit, and the
> Protocol needs an adapter regardless — Phase 1's are module-level functions
> and the interface is method-based. `sqlite_impl.py` is that adapter, and it
> is about twenty lines.

**Design notes:**

- **Container Apps over App Service** — App Service's free F1 tier is 32-bit only with a 60 CPU-minute daily cap; pandas and Streamlit will not fit comfortably. Container Apps scales to zero and gives 180,000 vCPU-seconds free per month, which is far more headroom.
- **GitHub Container Registry over ACR** — see B8. GHCR is free for public images and Container Apps pulls from it without special configuration.
- **Managed identity over connection strings** — the Container App authenticates to Azure SQL as itself. No password exists anywhere, so no password can leak from the repo. This is also the single most "senior" thing in the project.
- **OIDC federated credentials over a service principal secret** — GitHub Actions gets a short-lived token per run. No `AZURE_CREDENTIALS` secret to rotate or leak.
- **Container Apps Job over an Azure Function** — the ETL is a batch process that runs for minutes and needs pandas plus an ODBC driver. That is a container-shaped problem, not a function-shaped one.
- **Both SQL dialects kept behind one interface** — lets `pytest` run against SQLite in CI with no cloud dependency, while production uses Azure SQL. Phase 1's suite keeps running against SQLite untouched; the Azure implementation gets its own suite, skipped when no database is reachable, exactly as `tests/test_pipeline_numbers.py` already skips when `data/raw/` is absent.

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
- Compatibility level **160 or higher**. New Azure SQL databases default to it, so this is an assertion rather than a change — but `salary_trend`'s `WINDOW` clause is rejected outright below 160, and that failure at Step 8 looks like a syntax error in your SQL rather than a database setting

Verify the last one explicitly, because it is the only item here that a default
silently satisfies today and could stop satisfying later:

```sql
SELECT name, compatibility_level FROM sys.databases WHERE name = DB_NAME();
```

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

**Upload the Parquet conversions from Phase 1, not the raw `.xlsx`.** They are
already built, in `data/interim/`, one per source file, written by
`src/ingest.py`. The real numbers: nine `.xlsx` totalling **851 MB** convert to
nine `.parquet` totalling **175 MB**, so Parquet is about 4.9x smaller here, not
the 10x this plan guessed. Both fit the free 5 GB; only one is worth the upload
time.

Two cautions carried over from Phase 1 and worth repeating in `blob.py`:

- The FY2026 file is published as `LCA_Dislclosure_Data_FY2026_Q2.xlsx` —
  DOL misspelled "Disclosure". `ingest.source_files` globs rather than naming
  files for this reason; `blob.py` must not reintroduce a hardcoded name.
- The Parquet cache is the raw source columns as strings, pre-clean. A cache
  built from a different set of source files produces different headline
  numbers with every cleaning rule still passing. Upload the cache and the
  `data-sources.md` row counts together, and check them at load.

**Done when:** `az storage blob list -c raw` shows nine blobs totalling ~175 MB, and `download_raw` round-trips a file locally.

---

### Step 8 — T-SQL schema and the dialect split
**Files:** `sql/schema_azure.sql`, `src/db/base.py`, `src/db/sqlite_impl.py`, `src/db/azure_impl.py`, `src/db/__init__.py`

Leave `src/queries.py` where it is. Define the **seven** function signatures in `base.py` as a `Protocol` (see §6). `sqlite_impl.py` is a class delegating to `src/queries.py`. Write `azure_impl.py` with T-SQL equivalents. `__init__.py` exposes `get_backend()` reading the `DB_BACKEND` environment variable, defaulting to `sqlite`.

Dialect differences that will bite: `LIMIT` becomes `TOP` or `OFFSET/FETCH`, `TEXT` becomes sized `NVARCHAR`, and the hand-rolled percentile CTEs collapse into native `PERCENTILE_CONT`. **Read §6's three subsections before writing any of it** — four of the seven queries are parse errors in T-SQL for reasons the one-line summary above does not cover, and `PERCENTILE_CONT` does not substitute for a `GROUP BY` median the way it looks like it should.

> **Corrected.** Two claims in the original version of this step were wrong in
> ways that change the work.
>
> **There are seven functions, not five.** `wage_distribution` and
> `fiscal_years` were added during Phase 1's Step 8 because the dashboard needed
> a histogram and a year picker that the other five cannot feed. `app.py` calls
> all seven. A Protocol listing five compiles, passes review, and fails at
> runtime on the two the interface forgot.
>
> **`INTEGER PRIMARY KEY` does not become `IDENTITY(1,1)`.** Every primary key
> in this schema is assigned by the loader, not by the database:
> `load._write_lookup` numbers each lookup with `range(len(values))`, and
> `case_serial` is parsed out of the case number. An `IDENTITY` column refuses
> an explicit insert without `SET IDENTITY_INSERT ON` per table per session, and
> silently renumbers everything if you let it generate instead — which detaches
> every foreign key in `filings` from the row it pointed at. Declare the keys as
> plain `INT` / `BIGINT PRIMARY KEY` and keep assigning them in Python, exactly
> as Phase 1 does.

**Done when:** the existing Phase 1 suite passes against `DB_BACKEND=sqlite` with no test edited and the same count collected as before the change, and every function in `azure_impl.py` returns results **equal, not merely identically shaped**, to the SQLite backend for the same arguments — see the percentile note in §6, which is what makes exact comparison possible.

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

`load_azure.py` runs Phase 1's `ingest.load_all` → `clean.clean` over the blobs, writes the cleaned frame to `curated/`, then bulk inserts with `cursor.fast_executemany = True` and batches of 10,000. Row-by-row inserts of 850,321 rows will take hours and burn the compute grant — this flag is not optional.

Load order is forced by the foreign keys: the five lookups first, `filings`
last. `load.LOOKUPS` already lists them in a usable order; reuse it rather than
restating it, so the two backends cannot drift apart on which lookups exist.

Build locally, push to `ghcr.io/justinrheydavid/h-1b-tech-salary-explore-etl:latest` — GHCR paths must be lowercase, so the repo's capitalization does not survive into the image name.

**Done when:** `az containerapp job start` completes successfully and Azure SQL returns **850,321** filings, 43,573 employers, 123,990 titles, 8,570 locations — the last of which is the number that proves the collation fix in §6 held.

---

## Phase D — Application

### Step 10 — Containerize the dashboard
**Files:** `Dockerfile.web`, `app.py` (one-line change)

`app.py` swaps `from src import load, queries` for `get_backend()`, and the seven `_cache(queries.x)` wrappers bind to the backend's methods instead. The layout, the charts and the copy do not change — that is the payoff for the Step 8 interface split.

> **Corrected: this is not "a one-line change".** Three things in `app.py` name
> SQLite specifically, and the third is the one that matters.
>
> - Six `_cache(queries.…)` wrappers plus `_cache(queries.fiscal_years)`.
> - `queries.DEFAULT_JOB_TITLE`, read twice in `sidebar()`. It belongs on the
>   Protocol, not on one implementation.
> - `DATABASE_TROUBLE = (FileNotFoundError, IsADirectoryError,
>   sqlite3.DatabaseError)`, and a second `except pd.errors.DatabaseError`.
>   These are what stand between a visitor and a traceback, and **none of them
>   catch `pyodbc.Error`.** Ported carelessly, every Azure failure — the paused
>   database in §8's risk table above all — reaches the browser as a stack
>   trace, on the exact path Phase 1 spent a commit making readable. The
>   backend must own its own exception tuple and expose it.
>
> Budget an evening, not a line. `tests/test_app.py` drives the real script
> through Streamlit's `AppTest`; run it against both backends.

Dockerfile.web needs the same ODBC driver plus `streamlit run app.py --server.port=8501 --server.address=0.0.0.0`. Copy `.streamlit/config.toml` into the image — it carries the theme added at the end of Phase 1, and the app looks wrong without it. Add a `/_stcore/health` health probe.

Set `DB_BACKEND=azure` and `AZURE_SQL_SERVER` as environment variables on the container app.

**Done when:** the Container Apps FQDN serves the dashboard, a filter interaction returns correct data from Azure SQL, and the app cold-starts from zero replicas in under 30 seconds.

---

## Phase E — Automation and documentation

### Step 11 — GitHub Actions with OIDC
**Files:** `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`

Set up federated credentials: an Entra app registration with a federated credential scoped to `repo:JustinRheyDavid/H-1B-Tech-Salary-Explore:ref:refs/heads/main`, granted Contributor on `rg-prevailing`. The subject string is matched exactly and is case-sensitive; a wrong repo slug fails at token exchange with `AADSTS700213`, which does not name the subject it rejected. Store only `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` as repo variables — **no client secret exists**.

- `ci.yml`: on pull request — `pytest` against SQLite, plus `ruff`. `data/h1b.db` is committed, so the query and app suites run in CI with no setup; `tests/test_pipeline_numbers.py` skips itself, since `data/raw/` is gitignored. That skip is correct in CI and is also the one that lets a wrong Parquet cache through locally — the runbook should say so.
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

> **Rewritten 2026-08-13.** The schema below is a port of what `src/load.py`
> actually builds. The previous version of this section described three tables
> with `job_title`, `worksite_city`, `worksite_state` and `case_status` stored
> inline on `filings` — the design Phase 1 measured at 148 MB and abandoned at
> its own Step 6, above GitHub's 100 MB single-file limit. Porting that schema
> would mean migrating to a shape the source database has never had, and the
> row counts would not reconcile against anything.

### T-SQL schema

Six tables and a view, matching `load.SCHEMA` one for one.

```sql
-- The collation matters more than anything else on this page; see below.
CREATE TABLE employers (
    employer_id     INT           NOT NULL PRIMARY KEY,
    employer_name   NVARCHAR(120) COLLATE Latin1_General_BIN2 NOT NULL UNIQUE,
    raw_name_sample NVARCHAR(200) NULL
);

CREATE TABLE occupations (
    soc_id    INT          NOT NULL PRIMARY KEY,
    soc_code  NVARCHAR(10) NOT NULL UNIQUE,
    soc_title NVARCHAR(80) NOT NULL
);

CREATE TABLE titles (
    title_id  INT           NOT NULL PRIMARY KEY,
    job_title NVARCHAR(100) COLLATE Latin1_General_BIN2 NOT NULL UNIQUE
);

CREATE TABLE locations (
    location_id    INT          NOT NULL PRIMARY KEY,
    worksite_city  NVARCHAR(60) COLLATE Latin1_General_BIN2 NOT NULL,
    worksite_state NVARCHAR(2)  COLLATE Latin1_General_BIN2 NOT NULL,
    CONSTRAINT uq_locations UNIQUE (worksite_city, worksite_state)
);

CREATE TABLE visa_classes (
    visa_class_id INT          NOT NULL PRIMARY KEY,
    -- 20, not 10. The stored values are the full descriptions, not the short
    -- names this plan's prose uses: 'H-1B1 Singapore' is 15 characters,
    -- 'E-3 Australian' 14, 'H-1B1 Chile' 11, 'H-1B' 4. At NVARCHAR(10) the
    -- load dies on error 8152 after the other four lookups have committed.
    visa_class    NVARCHAR(20) NOT NULL UNIQUE
);

CREATE TABLE filings (
    -- BIGINT, not INT. The largest serial in the data is 26,085,730,937,
    -- twelve times INT's ceiling of 2,147,483,647. INT here fails the load
    -- with an arithmetic overflow partway through, after the lookups have
    -- already committed.
    case_serial     BIGINT   NOT NULL PRIMARY KEY,
    case_prefix     SMALLINT NOT NULL,

    employer_id     INT      NOT NULL REFERENCES employers(employer_id),
    soc_id          INT      NOT NULL REFERENCES occupations(soc_id),
    title_id        INT      NOT NULL REFERENCES titles(title_id),
    location_id     INT      NULL REFERENCES locations(location_id),
    visa_class_id   INT      NOT NULL REFERENCES visa_classes(visa_class_id),

    -- INT, not DECIMAL(12,2). Phase 1 rounds to whole dollars before it
    -- writes; storing cents here would invent a precision the data does not
    -- have and would make the two backends compare unequal on rounding alone.
    annual_wage     INT      NULL,
    annual_from     INT      NULL,
    annual_to       INT      NULL,
    prevailing_wage INT      NULL,

    fiscal_year     SMALLINT NOT NULL,
    full_time       BIT      NOT NULL,
    withdrawn       BIT      NOT NULL,   -- 1 = 'Certified - Withdrawn'

    is_outlier      BIT      NOT NULL DEFAULT 0,
    pw_outlier      BIT      NOT NULL DEFAULT 0,
    unit_repaired   BIT      NOT NULL DEFAULT 0,
    pw_repaired     BIT      NOT NULL DEFAULT 0
);

-- The two columns the queries filter on, matching Phase 1's index choice.
-- The INCLUDE columns are a hypothesis, not a measured win: Phase 1 measured
-- four covering indexes on annual_wage at +58 MB for no improvement at all,
-- because SQLite sorts for a window function whether or not an index could
-- supply the order. Azure SQL may or may not behave differently, and these
-- queries join `locations` regardless, so the table is touched either way.
-- Build them, measure, and drop the INCLUDE lists if they earn nothing.
CREATE INDEX idx_filings_title    ON filings(title_id)
    INCLUDE (annual_wage, location_id, fiscal_year, is_outlier);
CREATE INDEX idx_filings_location ON filings(location_id)
    INCLUDE (annual_wage, title_id, fiscal_year, is_outlier);

CREATE VIEW v_filings AS ...;  -- same body as load.SCHEMA, FORMAT() for printf()
```

**Collation is the migration's one hard failure, and this plan previously had
it backwards.** The dialect table used to say a title search for
`"data analyst"` "returns nothing in SQLite but matches in Azure SQL". That
stopped being true at Phase 1's Step 7: every filter in `queries.py` matches
`COLLATE NOCASE`, and `title_search` groups on `lower(job_title)`. Both
backends already match case-insensitively, so nothing is broken at *query*
time.

The break is at *load* time, and it runs the other way. `titles.job_title` is
`UNIQUE`, and in SQLite that uniqueness is byte-exact, so `Software Engineer`,
`SOFTWARE ENGINEER` and `software engineer` are three legitimate rows. Azure
SQL's default collation makes them one — and it does the same to
`Data Engineer` and `Data Engineer ` , because SQL Server's string comparison
also ignores **trailing spaces**. Two rules, not one. Measured against the
shipped database:

| | rows | distinct, case only | distinct, case + trailing space |
|---|---:|---:|---:|
| `titles` | 123,990 | 117,391 | **114,704** |
| `employers` | 43,573 | 43,573 | 43,573 |
| `locations` | 8,570 | 8,570 | 8,570 |

**9,286 titles collide** — 6,599 by case alone, and 2,687 more once trailing
spaces stop counting. `Software Engineer` has eight spellings and
`Software Development Engineer in Test` thirteen; separately, 4,233 titles end
in a space, so `' Data Engineer'`, `' Data Engineer '` and `' DATA ENGINEER'`
become one row. Under the default collation the `UNIQUE` constraint rejects all
9,286 with duplicate-key errors, and the only alternatives are dropping them —
losing the filings that point at them — or silently merging title ids, which
changes every per-title figure the README quotes.

> **An earlier pass of this reconciliation quoted 6,599**, having measured case
> and stopped there. The fix below is unchanged, but the figure was wrong by
> 2,687 in the direction that makes the problem look smaller. Trailing spaces
> are the second rule, and 118 titles end in a *tab*, which SQL Server does
> **not** ignore — only spaces.

`BIN2` on the three columns that need it is intended to fix both at the schema
level: a binary collation compares byte by byte, so neither case nor trailing
spaces are folded away, the data ports one for one, and case-*in*sensitive
matching moves into the queries where Phase 1 already put it —
`WHERE job_title = ? COLLATE Latin1_General_CI_AS`. `employers` and `locations`
show no collisions under either rule because `clean.py` normalizes them; they
get `BIN2` anyway, so that a future normalization change cannot quietly start
merging rows.

**Verify `BIN2` before Step 9 depends on it.** That a binary collation makes
*case* significant is documented. That it also makes *trailing spaces*
significant is the half this plan could not confirm from Microsoft's
documentation — the comparison-operator reference does not discuss trailing
spaces at all, and the behaviour is attested mainly in community write-ups. It
is one insert to settle, and Step 4 is where to settle it:

```sql
CREATE TABLE #collation_check (t NVARCHAR(100) COLLATE Latin1_General_BIN2 UNIQUE);
INSERT #collation_check VALUES (N'Data Engineer');
INSERT #collation_check VALUES (N'Data Engineer ');   -- trailing space
INSERT #collation_check VALUES (N'DATA ENGINEER');    -- case
SELECT COUNT(*) FROM #collation_check;                -- must be 3, not 1
DROP TABLE #collation_check;
```

If that returns anything but 3, stop: `BIN2` is not sufficient, and the fallback
is to make the loader's title keys unique some other way — normalizing the
whitespace on the way in, which changes what users search on and needs its own
decision — rather than pressing ahead and losing rows.

The counts above were measured against `data/h1b.db` at `acb12af`. Re-run them
after any rebuild.

### Dialect differences to expect

| Concern | SQLite (Phase 1) | Azure SQL (Phase 2) |
|---|---|---|
| Primary keys | `INTEGER PRIMARY KEY`, assigned by the loader | plain `INT`/`BIGINT PRIMARY KEY`, still assigned by the loader — **not** `IDENTITY` |
| Key width | `INTEGER` is 8 bytes | `case_serial` needs `BIGINT`; `INT` overflows at 2.1 billion |
| Strings | `TEXT`, unbounded | `NVARCHAR(n)`, sized from the data: title 60 chars max, employer 89, city 35, soc_title 56 |
| Money | `INTEGER`, whole dollars | `INT`, whole dollars — keep it, do not "improve" to `DECIMAL` |
| Booleans | `INTEGER` 0/1 | `BIT` |
| Row limiting | `LIMIT 20` | `SELECT TOP 20` or `OFFSET 0 ROWS FETCH NEXT 20 ONLY` |
| Percentiles | ranked in SQL by hand, linear interpolation | `PERCENTILE_CONT` — same definition, **but window-only**; see below |
| `GROUP BY` strictness | bare columns and select aliases both permitted | every non-aggregate selected column must be in `GROUP BY`; no aliases in `GROUP BY` or `HAVING`. **Four queries break; see below** |
| Two-argument `max` | `max(0.0, x)` is a scalar function | `MAX` is aggregate-only and rejects two arguments — use `GREATEST(0.0, x)` |
| Case matching | `COLLATE NOCASE` in the query | `COLLATE Latin1_General_CI_AS` in the query; `BIN2` on the unique columns |
| `LIKE` case sensitivity | **case-insensitive for ASCII by default** | follows the column's collation, so `BIN2` makes it case-**sensitive**; see below |
| String concat in `LIKE` | `? \|\| '%' ESCAPE '\'` | `? + '%' ESCAPE '\'` |
| Named windows | `WINDOW years AS (...)` supported | supported, but requires **database compatibility level 160+** |
| Parameters | `?` | `?` via pyodbc (same) |
| Bulk insert | `executemany` | `fast_executemany = True`, batches of 10,000 |

### The four queries that will not parse in T-SQL

SQLite permits a bare column in a `GROUP BY` query and an alias in `GROUP BY` or
`HAVING`. SQL Server permits neither, and Phase 1 uses both freely. Every one of
these is a parse error, not a wrong answer, so they surface immediately — but
budget for four rewrites rather than meeting them one at a time:

| Function | What it does | Why T-SQL refuses |
|---|---|---|
| `top_employers` | selects `e.employer_name`, groups by `r.employer_id` | error 8120 — column not in an aggregate or the `GROUP BY` |
| `title_search` | selects `t.job_title`, groups by `lower(t.job_title)` | error 8120, and `ORDER BY t.job_title` goes with it |
| `salary_by_city` | `HAVING n_filings >= ?` | `n_filings` is a select alias; repeat `MAX(n)` instead |
| `wage_distribution` | `GROUP BY bin_floor` | `bin_floor` is a select alias; repeat the `CAST(...)` expression |

Adding the grouping column to `GROUP BY` is the fix in the first two cases —
`GROUP BY r.employer_id, e.employer_name` is equivalent here because
`employer_id` is the key, and `GROUP BY lower(t.job_title)` needs an
`MIN(t.job_title)` or similar to pick the representative spelling that Phase 1's
bare column picks arbitrarily today.

### `PERCENTILE_CONT` is not a drop-in for a `GROUP BY` median

The dialect table above used to imply it was, which would have cost an evening.
`PERCENTILE_CONT` requires an `OVER` clause, has no plain-aggregate form, and
returns **one value per row rather than one per group** — Microsoft's reference
is explicit that the `ORDER BY` and rows/range parts of `OVER` cannot be
specified, because it is strictly a window function.

`top_employers`, `salary_by_city` and `salary_trend` each want one median per
group, so each needs `SELECT DISTINCT` over the partitioned form, or the window
computed in a subquery and collapsed outside it. `salary_percentiles` is the
easy one: a single row already, so `OVER ()` with no partition works directly.

### `BIN2` makes `LIKE` case-sensitive, which breaks `title_search`

This follows from §6's collation fix and is easy to miss, because it is a
behavioural change rather than an error. SQLite's `LIKE` is case-insensitive for
ASCII by default — verified: `SELECT 'DATA ANALYST' LIKE 'data%'` returns 1 —
which is why `title_search("data")` finds `Data Analyst`, `DATA ANALYST` and
`data analyst` alike today.

In Azure SQL, `LIKE` follows the column's collation. With `BIN2` on
`titles.job_title`, typing "data" matches nothing but an exact-case prefix, and
the autocomplete quietly stops working for most of what people type. The `LIKE`
needs its own `COLLATE Latin1_General_CI_AS`, in the one query that runs on
every keystroke:

```sql
WHERE t.job_title COLLATE Latin1_General_CI_AS LIKE ? + '%' ESCAPE '\'
```

`BIN2` is still the right choice — it is what keeps the 9,286 duplicate-by-
collation titles loadable at all — but it moves case-insensitivity from a
default into something each query states.

**`_escape_like` is complete for SQLite and incomplete for T-SQL.** It escapes
`\`, `%` and `_`, which is every wildcard SQLite's `LIKE` has. T-SQL adds a
character class: `[abc]` matches one of three characters, and `[a-z]` a range.
**945 titles contain `[`** — `Network Protocol Engineer [Senior]`,
`Sr Business Systems Analyst [00058036]`, `Staff Software Developer - Telemetry
-[KBGFJG152072-1]`. A prefix typed into the search box that reaches one of them
is read as a pattern rather than as text, and the two backends return different
rows for the same keystrokes. The Azure implementation needs `[` in its escape
set; the SQLite one must not have it, or it would escape a character that is
already literal there. That makes `_escape_like` the one piece of Phase 1 code
that genuinely has to fork per backend.

One more divergence in the same query, smaller and not worth code: `title_search`
groups on `lower(job_title)`, and SQLite's `lower()` folds ASCII only where
T-SQL's `LOWER()` is Unicode-aware. 3,462 titles carry non-ASCII characters
(mostly en-dashes and stray tabs, a few accented letters), so a handful of
groups differ between backends. Worth knowing before someone treats a diff in
`title_search` output as a porting bug.

**On percentiles, the good news.** This plan used to say SQLite "does not
support" them and that Phase 1 "computes them in pandas". Neither is what
happened: `queries._percentile` ranks with `ROW_NUMBER()`, counts with
`COUNT(*) OVER ()`, and interpolates between the two rows either side of
`1 + fraction * (n - 1)` — which is `PERCENTILE_CONT`'s definition exactly, and
the same one pandas, numpy and Excel use. Phase 1 chose it deliberately over
the simpler "first row at or past the target", which biases every even-sized
group downward and moved 34% of city medians by up to $12,850.

So the two backends should agree to the dollar, not approximately, and Step 8's
acceptance can be an equality assertion over a few hundred slices rather than a
shape check. That is a much stronger test, and it is available for free because
Phase 1 already did the work.

Checked rather than assumed: the SQL expression was evaluated against numpy's
linear interpolation for p25, p50 and p75 on three real title slices — 4,380,
70,943 and 6,616 filings, covering both even and odd row counts — and agreed on
all nine to a delta of 0.000000. The definitions match. What remains to verify
is that Azure SQL's implementation matches the definition, which is Step 8's job
and cannot be checked from here.

### Backend interface

Seven methods. Every Phase 1 function also takes `db: Path | None`, which is how
the tests point at a small database; on the interface that becomes construction
state, not a per-call argument.

```python
# src/db/base.py
class Backend(Protocol):
    DEFAULT_JOB_TITLE: str          # the sidebar opens on it; see below
    TROUBLE: tuple[type[BaseException], ...]   # what app.py must catch

    def salary_percentiles(self, job_title=None, city=None, state=None,
                           fiscal_year=None, include_outliers=False) -> DataFrame: ...
    def wage_distribution(self, job_title=None, city=None, state=None,
                          fiscal_year=None, include_outliers=False,
                          bin_width=10_000) -> DataFrame: ...
    def top_employers(self, job_title=None, city=None, limit=20) -> DataFrame: ...
    def salary_by_city(self, job_title=None, min_filings=10) -> DataFrame: ...
    def salary_trend(self, job_title=None, city=None) -> DataFrame: ...
    def title_search(self, prefix="", limit=25) -> list[str]: ...
    def fiscal_years(self) -> list[int]: ...

# src/db/__init__.py
def get_backend() -> Backend:
    """DB_BACKEND: 'sqlite' (default) | 'azure'"""
```

Two members are not functions and both earn their place:

- **`DEFAULT_JOB_TITLE`.** Phase 1 exists on the filtered path by design —
  unfiltered, every query ranks all 850,321 rows and takes 0.7–1.6 seconds, and
  four covering indexes on `annual_wage` were measured at +58 MB for no
  improvement. Azure SQL's `INCLUDE` columns may well change that, and it is
  worth measuring. Until measured, the dashboard opens on a title on both
  backends.
- **`TROUBLE`.** The exception tuple `app.py` catches, so the error path is not
  hardcoded to `sqlite3`. See Step 10.

`title_search` and `fiscal_years` return lists, not DataFrames — matching Phase
1, and matching what `app.py` feeds straight into a `selectbox`.

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
| Azure SQL Database | 100,000 vCore-sec + 32 GB/month, lifetime | ~3,000 vCore-sec; the SQLite file is 78 MB, so call it ~150 MB with T-SQL's index `INCLUDE` columns | $0.00 |
| Container Apps (web) | 180,000 vCPU-sec + 2M requests/month | ~5,000 vCPU-sec at `minReplicas: 0` | $0.00 |
| Container Apps (ETL job) | shares the same grant | ~1,800 vCPU-sec/month | $0.00 |
| Container Apps environment | free when Consumption-only, no VNet | — | $0.00 |
| Blob Storage | 5 GB LRS hot | 175 MB Parquet, measured | $0.00 first 12 months |
| Bandwidth out | 100 GB/month free | negligible | $0.00 |
| GitHub Container Registry | free for public images | 2 images | $0.00 |
| GitHub Actions | free for public repos | ~200 min/month | $0.00 |
| Entra ID app registration | free | 1 | $0.00 |
| **Total** | | | **$0.00** |

**The three ways this becomes non-zero, in likelihood order:**

1. Creating an Azure Container Registry out of habit — ~$5/month, and Basic ACR has no free tier. Use GHCR. This is B8 and it is the most likely mistake.
2. Setting `minReplicas: 1` on the web app to avoid cold starts — turns idle into a billable state. Accept the 20–30 second cold start instead.
3. Blob Storage after month 12 — sources disagree on whether the 5 GB is permanently free or 12-month only. Assume 12-month. At 175 MB the worst case is under $0.01/month, but set a calendar reminder for month 11 to check.

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
| **Collation collapses the `titles` load** | **High if §6's `BIN2` is dropped** | 9,286 of 123,990 titles are duplicates under Azure SQL's default comparison — 6,599 by case, 2,687 more by trailing space — and the `UNIQUE` constraint rejects them. Not a subtlety and not recoverable by retry: the fix belongs in the DDL, before the first load. Run §6's `BIN2` check at Step 4, and assert `SELECT COUNT(*) FROM titles = 123990` at the end of Step 9. |
| **`IDENTITY` renumbers the lookup keys** | Medium | Every key is assigned by the loader. An `IDENTITY` column either refuses the explicit insert or generates its own, detaching every foreign key in `filings` from the row it pointed at — and the load *succeeds*, so the dashboard shows wrong employers against right wages. Plain `INT PRIMARY KEY`, per §6. |
| **850,321 row load exceeds job timeout** | Low-medium | `replicaTimeout: 3600` gives an hour, ample with `fast_executemany`. Fallback: load one fiscal year per job execution. |
| **Migration never finishes and Phase 1 rots** | Medium — the real project risk | Phase 1 stays deployed and is the résumé link. Azure is strictly additive. If Phase E never happens, nothing is lost. |

---

## 9. Open questions

1. **Which Azure link is the primary demo?** Recommendation: keep Streamlit Cloud as the link on the résumé (always warm, instant), and present Azure as "also deployed on Azure with IaC and CI/CD" with its own link in the README. Best of both.
2. **Region.** `eastus` has the widest service availability and lowest chance of a free-tier capacity error. Any reason to prefer somewhere closer to you?
3. **Is the ETL job worth containerizing at all, versus running the load from GitHub Actions?** Actions would be simpler and equally free. The Container Apps Job is meaningfully more Azure-native — which is what you said you wanted — but it is roughly two extra evenings. Confirm you still want it.
4. **Scheduled refresh — yes or no?** DOL publishes quarterly, so a monthly cron is mostly idle. It is three lines of Bicep and demonstrates orchestration. Recommendation: yes, as Step 12's optional extra.
5. **Do you want a `dev` and `prod` environment split?** Realistic, and doubles both resource count and free-tier consumption. Recommendation: no. One environment, and say so in the README rather than pretending otherwise.
6. **Resource naming — `prevailing-*` or `h1b-*`?** This plan was drafted when the project was going to be called Prevailing. It is called the H-1B Tech Salary Explorer everywhere a person can see: the repo, the page title, the README. The Azure resource names are the last place the old name survives, and they are cheap to change now and annoying to change after Step 11's federated credential and Step 6's manual SQL grants both name them. Recommendation: rename to `rg-h1b`, `h1b-web`, `h1b-etl` at Step 3, before anything exists. Decide before Phase B.

---

## Handoff

Phase 1 has shipped, so this plan is unblocked. The coder should work Phase A → E in order, and must not skip Step 2. If Step 4's free-tier flags cannot be set as written (Azure occasionally gates the free offer per subscription), stop and revise this plan rather than deploying a billable SKU "temporarily".

Steps 1, 2, 4 and 6 cannot be automated from here: they need a browser session
in the Azure portal, a credit card at signup, an alert email actually arriving,
and a T-SQL grant run as the Entra admin. Everything in Phases C, D and E is
code and can be written and reviewed before an account exists — but not
verified, so do not mark those steps done on the strength of a clean read.

Two questions in §9 want answering before Phase B rather than during it: the
region (§9.2) and the resource naming (§9.6). Both are cheap now and both are
named in a federated credential and a manual SQL grant later.
