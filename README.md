# H-1B Tech Salary Explorer

What US tech jobs actually pay, from 850,321 wage figures employers filed with
the Department of Labor — searchable by job title, city and year.

### ▶ [Open the live dashboard](https://h-1b-tech-salary-explore-morzlyjgltmfnutdx7pa68.streamlit.app/) &nbsp;·&nbsp; [the same app, running on Azure](https://h1b-web.calmwave-8f560d92.canadacentral.azurecontainerapps.io/)

Two deployments of one codebase. The first reads a SQLite file committed to this
repository; the second reads Azure SQL from a container, and takes a few seconds
to wake up because it scales to zero when nobody is looking.

---

## Why this data

Most salary sites report what people say they earn. This reports what employers
legally committed to pay, on federal forms, under penalty of perjury. Every
figure comes from a Labor Condition Application filed with the DOL Office of
Foreign Labor Certification before hiring on an H-1B, E-3 or H-1B1 visa.

It is public, exact rather than banded, and genuinely messy — mixed pay units,
inconsistent employer names, column headers that change between fiscal years,
and 73% of the rows blank. Cleaning it is most of the work, and the
[cleaning decisions](#data-cleaning-decisions) below are the part worth reading.

## Status

Complete and deployed twice. **Phase 1** built the application in ten steps
against [`docs/plans/h1b-salary-explorer.md`](docs/plans/h1b-salary-explorer.md);
**Phase 2** moved it onto Azure in twelve more, against
[`docs/plans/azure-migration.md`](docs/plans/azure-migration.md), without the
application code learning that it had moved.

- [x] 1. Repo skeleton and dependencies
- [x] 2. Data acquisition
- [x] 3. Exploration notebook
- [x] 4. Cleaning module
- [x] 5. Tests
- [x] 6. Database schema and loader
- [x] 7. Query layer
- [x] 8. Streamlit dashboard
- [x] 9. Deploy
- [x] 10. Documentation

Phase 2 — Azure:

- [x] 1–2. Account, spend guardrails
- [x] 3–5. Storage, Azure SQL, Container Apps
- [x] 6. Managed identities and grants
- [x] 7–8. Raw data in Blob, T-SQL schema
- [x] 9. ETL job — loads 850,321 filings from a container
- [x] 10. Dashboard reads Azure SQL
- [x] 11. CI and deployment from GitHub, with no stored secret
- [x] 12. Runbook, README, teardown

## Data sources

Nine quarterly files from the [DOL OFLC performance data page](https://www.dol.gov/agencies/eta/foreign-labor/performance),
under **Disclosure Data → LCA Programs (H-1B, H-1B1, E-3)**, downloaded
6 August 2026. Together they cover **October 2023 to March 2026** — ten
consecutive quarters, no gaps.

| Stage | Rows |
|---|---:|
| Rows in the nine spreadsheets | 4,978,487 |
| Blank padding rows, dropped on read | −3,610,511 |
| Duplicate case numbers, deduplicated | −20,873 |
| Not certified (denied or withdrawn) | −31,304 |
| Not a tech occupation | −465,478 |
| **Filings in the database** | **850,321** |

Per-file row counts and the record layouts are in
[`docs/data-sources.md`](docs/data-sources.md).

## Data cleaning decisions

Seventeen defects were measured in
[`notebooks/01_exploration.ipynb`](notebooks/01_exploration.ipynb) before any
cleaning code was written. These are the decisions that change the numbers.

**Wages are annualized, and 3,221 filings had the wrong unit.** Filings state
an amount and a unit — Year, Month, Bi-Weekly, Week or Hour. Some state an
annual salary against an hourly unit. A figure that is implausible once scaled
by its unit but plausible taken as-is is treated as already annual. Left alone,
the maximum wage is $1.47 billion and the mean is $428,938 against a median of
$118,248. The decision is made once per filing from the low end of the band, so
a band can never end up with its two sides on different scales.

**A third of filings give a range, not a figure — this reports the midpoint.**
`WAGE_RATE_OF_PAY_TO` is populated on 32% of rows with a median spread of 22%.
Reading only the lower bound understates those salaries by 13.8%. Both ends are
kept in the database. The metric is labelled *offered wage* rather than
*salary*, because that is what it is.

**Only certified filings count.** Denied and withdrawn cases are 7.5% of rows
and never became an offer. `Certified` and `Certified - Withdrawn` are kept —
the second means the employer certified and later withdrew, so the wage was
still committed to.

**Outliers are flagged, never deleted.** 37 tech filings annualize below
$10,000 or above $2,000,000. They stay in the database with `is_outlier = 1`
and are excluded from every figure by default, with a toggle to include them.
The flag is set on the *reported midpoint*, not the lower bound — flagging the
lower bound misses 84 filings whose floor is plausible but whose midpoint is
not.

**The prevailing wage is repaired and flagged separately.** It carries the same
unit defect, and worse: nine filings put it at up to $360,056,320. Four of them
pair that with a perfectly ordinary offered wage, so an outlier check based on
the offered wage alone lets them through. Two columns, two flags.

**Employer normalization is deliberately conservative** — case, punctuation and
one trailing legal suffix, nothing else. `MICROSOFT CORPORATION` and
`Microsoft Corp.` become one employer. No fuzzy matching: `SparkCognizant Inc`
and `TMG HEALTH - A COGNIZANT COMPANY` are unrelated to Cognizant Technology
Solutions, and a similarity threshold that merges the first two also merges the
second two.

**Job titles stay exactly as filed, and are matched case-insensitively.**
Normalizing them would destroy what people search on; 3,587 filings say
`Data Analyst` and 777 say `DATA ANALYST`, so an exact match loses 17% of them.
`soc_title` provides the clean grouping instead.

**Order matters.** Repair units → annualize → take the midpoint → flag
outliers. Any other order lets bad data through a later gate.

## Architecture

```
  DOL .xlsx files  ──▶  ingest  ──▶  clean  ──▶  load  ──▶  h1b.db  ──▶  Streamlit app
   (data/raw/)                                            (SQLite)      (deployed)
```

One direction only. `clean.py` holds every normalization decision and touches
neither the database nor the network; `queries.py` holds every SQL statement
the dashboard runs; `app.py` contains no SQL at all.

The database is six tables — filings plus lookups for employer, occupation,
title, location and visa class — and is committed to the repo, so the deployed
app needs no credentials and no cold-start setup. That constraint shaped the
schema: the planned three-table version measured 148 MB, above GitHub's 100 MB
file limit. Lookup tables, integer wages and indexes only on the two columns
queries filter on bring it to 78 MB with all 850,321 filings intact.

327 tests cover the cleaning rules, the loader, the SQL, the dashboard —
exercised headlessly through Streamlit's `AppTest` — and the contract between
the Bicep templates and the code that reads them. 23 of them talk to live Azure
and are deselected by default.

## Architecture on Azure

The same code, deployed a second way. `DB_BACKEND` picks the backend; nothing
above `src/db/` knows which one it got.

```
 GitHub repo
   │
   ├── push to main ──▶ GitHub Actions ──┬──▶ build images ──▶ ghcr.io (free, public)
   │                    (OIDC, no secrets)│
   │                                      └──▶ az deployment (Bicep) ──▶ Azure
   │
   └─────────────────────────────────────────────────────────────────────┐
                                                                          ▼
  ┌──────────────────────────── Azure Resource Group: rg-h1b ───────────────────────────────────┐
  │                                                                                              │
  │   Blob Storage                 Container Apps Job              Azure SQL Database            │
  │   ┌──────────────┐   read      ┌──────────────────┐   write    ┌────────────────────┐        │
  │   │ raw/  *.pq   │ ──────────▶ │     h1b-etl      │ ─────────▶ │     sqldb-h1b      │        │
  │   │ curated/*.pq │ ◀────────── │ (manual trigger) │            │  serverless, free  │        │
  │   └──────────────┘   write     └──────────────────┘            │  auto-pause 60 min │        │
  │                                         │                      └────────────────────┘        │
  │                                  managed identity                        ▲                   │
  │                                                                          │ read              │
  │                                            Container App                 │                   │
  │                                            ┌──────────────────┐          │                   │
  │                                            │ h1b-web          │ ─────────┘                   │
  │                                            │ Streamlit,       │                              │
  │                                            │ min replicas = 0 │                              │
  │                                            └──────────────────┘                              │
  │                                                     │ HTTPS                                  │
  └─────────────────────────────────────────────────────┼──────────────────────────────────────┘
                                                        ▼
                                     https://h1b-web.<region>.azurecontainerapps.io
```

**There is no password anywhere in this diagram.** The SQL server has
`azureADOnlyAuthentication` on, so there is no password to steal; the storage
account has `allowSharedKeyAccess` off, so there is no key. Both containers
authenticate with managed identities, and GitHub Actions authenticates to Azure
through OIDC — a token minted per run and trusted because of a federated
credential naming this repository and this branch. `gh secret list` is empty,
and that is the point.

### Why these services

Every choice here was made against one constraint: **the whole thing must cost
$0.00.** It does — the budget reads `0.00 CAD`.

- **Azure SQL Database, serverless free offer** — 100,000 vCore-seconds and
  32 GB per month. Free depends entirely on `autoPauseDelay: 60`: the database
  sleeps after an idle hour and stops consuming the grant. The cost is that the
  first connection after a quiet spell is *refused* while it resumes, which is
  why both the loader and the dashboard retry rather than treating that as an
  error.
- **Container Apps, consumption plan** — 180,000 vCPU-seconds and 360,000
  GiB-seconds free per month. `minReplicas: 0` is what keeps it there; set it to
  1 and this becomes the first line item on a bill.
- **Blob Storage** — the free grant covers the 183 MB of Parquet caches easily,
  and a lifecycle rule deletes `raw/` after 90 days so it cannot creep.
- **GitHub Container Registry, not Azure Container Registry** — ACR has no free
  tier and Basic is about $5/month. This is the single most likely way a project
  like this starts costing money, and it was decided before any code was
  written.
- **No Log Analytics workspace** — billable past 5 GB/month. The trade is real
  and it bit: when the ETL job failed, the container's log stream had already
  rotated most of the useful output away, so the diagnosis had to be rebuilt
  from one surviving line.

The interesting number is the one that is *not* free: a cold start from zero
replicas measured **32 seconds** from genuine idle, against a 30-second target.
The deployment reports that figure on every run and does not fail on it — a slow
dashboard is a working dashboard, and failing the deploy would remove the fast
fix, which is rolling back.

Checking that all of it is actually working is one command:

```bash
./scripts/health-check.sh          # add --full for the 23 live Azure tests
```

Standing it up in your own subscription, refreshing the data, and tearing it all
down are in [`docs/azure-runbook.md`](docs/azure-runbook.md).

## Running locally

```bash
git clone https://github.com/JustinRheyDavid/H-1B-Tech-Salary-Explore.git
cd H-1B-Tech-Salary-Explore && pip install -r requirements.txt
streamlit run app.py
```

The database is in the repo, so that is all you need. Requires Python 3.11+.
Rebuilding it from source spreadsheets — `python -m src.load` — needs the nine
files in `data/raw/`; see [`docs/data-sources.md`](docs/data-sources.md).

## Limitations

Worth knowing before quoting any of these numbers.

- **This is not a salary survey.** It covers only roles an employer sought to
  fill with sponsored workers. Employers large enough to run an immigration
  process are over-represented, and everyone not sponsored is absent.
- **Offered wage is not total compensation.** No equity, no bonus, no
  relocation. In tech that gap is large and varies most at the companies paying
  the most, so the top of the distribution is understated by more than the
  bottom.
- **A filing is not a hire.** It is a commitment made during an application.
  Some roles were never filled, and some were filled at a different figure.
- **Employer normalization is a heuristic.** Subsidiaries, acquisitions and
  staffing firms filing on a client's behalf are not resolved, so a large
  employer's filings may be split across several names.
- **FY2026 covers two quarters** where every other year covers four. Its filing
  count is not comparable with the others; its median is.
- **2,216 filings have no decision date** and fall back to their received date
  for the fiscal-year bucket — 0.16% of rows, dated by when they were filed
  rather than decided.

## License

Code under the MIT License. The underlying data is a public work of the US
federal government and is not subject to copyright.
