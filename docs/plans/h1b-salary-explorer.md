# H-1B Tech Salary Explorer — Build Plan

**Status:** ready to build
**Author:** architect agent
**Date:** 2026-08-04
**Estimated effort:** 1–2 weeks at a few hours per night (10 steps)

---

## 1. Goal

Build a portfolio project that proves both new certificates in one artifact: a Python pipeline that ingests raw US Department of Labor H-1B disclosure files, cleans them, loads them into a normalized SQL database, and serves a deployed Streamlit dashboard where anyone can look up real salaries by job title, employer, and city.

The point is a link Justin can put on a résumé. A recruiter clicks it, types "Data Analyst", picks Austin, and sees actual filed salaries from real employers — no setup, no notebook, no explanation needed. The repo behind it shows the cleaning logic and the SQL that made it possible.

Why this dataset: it is public, legal to use, genuinely messy (which is the point — cleaning it is the skill being demonstrated), contains exact salary figures rather than self-reported ranges, and is large enough (~600k rows/year) that naive pandas approaches break and real SQL earns its keep.

---

## 2. Assumptions

Recorded because the user did not specify them. Any of these can be overridden before Step 1 without redesigning the project.

| # | Assumption | Why |
|---|---|---|
| A1 | Python 3.11+, pandas, and git are already installed and working | Standard after the IBM coursework |
| A2 | The repo is public on GitHub under Justin's account | Required for free Streamlit Community Cloud deploys, and recruiters need to read the code |
| A3 | Scope is fiscal years 2024, 2025, and 2026-to-date — three years, ~1.8M rows | Enough for year-over-year trends without a multi-GB repo |
| A4 | Only `CERTIFIED` and `CERTIFIED-WITHDRAWN` cases are analyzed | Denied and withdrawn filings do not represent real offered wages |
| A5 | Only tech-adjacent SOC codes are kept (15-xxxx Computer/Mathematical, plus 11-3021 Computer & IS Managers) | Keeps the project focused on tech salaries; also cuts row count ~60% |
| A6 | All wages normalized to annual USD | Filings mix hourly/weekly/monthly/yearly; comparison is meaningless without this |
| A7 | No authentication, no user accounts, read-only dashboard | Nothing to protect; auth would be scope creep |
| A8 | English-only, US-only, no currency conversion | Source data is US-only by definition |
| A9 | Data refresh is manual (re-run one command when DOL posts a new quarter) | Scheduled refresh is a stretch goal, not v1 |

---

## 3. Out of scope for v1

Explicitly not building these. If they come up mid-build, they go in a `FUTURE.md`, not in the code.

- Scraping any job board or live posting site
- Machine learning salary prediction (tempting, but the certs are analysis + SQL, and a bad regression hurts the portfolio more than no regression)
- User accounts, saved searches, or any write path from the dashboard
- Real-time or scheduled data refresh
- Non-tech occupations
- Green card (PERM) data — different schema, different program, doubles the work
- Mobile-optimized layout beyond what Streamlit gives for free
- A REST API layer

---

## 4. Architecture

Four components, strictly one-directional. Raw files in, database out, dashboard reads. Nothing writes backwards.

```
  DOL .xlsx files  ──▶  ingest  ──▶  clean  ──▶  load  ──▶  h1b.db  ──▶  Streamlit app
   (data/raw/)                                            (SQLite)      (deployed)
```

| Component | Owns | Does not |
|---|---|---|
| `ingest.py` | Downloading/reading raw DOL files, nothing else | Clean, judge, or reshape data |
| `clean.py` | All normalization decisions (wages, titles, cities, filtering) | Touch the database or the network |
| `load.py` | Schema creation, indexes, inserting cleaned rows | Contain business logic |
| `queries.py` | Every SQL query the app runs, as named functions | Format or render anything |
| `app.py` | Layout, widgets, charts | Contain raw SQL or cleaning logic |

That separation is the reviewable part. A hiring manager skimming the repo should see cleaning logic isolated from presentation logic in under a minute.

### File tree

```
h1b-salary-explorer/
├── README.md                  # the actual portfolio artifact — see Step 10
├── requirements.txt
├── .gitignore                 # ignores data/raw/, keeps data/h1b.db
├── data/
│   ├── raw/                   # downloaded .xlsx, gitignored (large)
│   └── h1b.db                 # SQLite, committed (~80-150 MB after pruning)
├── src/
│   ├── ingest.py
│   ├── clean.py
│   ├── load.py
│   └── queries.py
├── notebooks/
│   └── 01_exploration.ipynb   # the "how I figured out the mess" notebook
├── tests/
│   └── test_clean.py
└── app.py                     # Streamlit entry point, must be at repo root
```

**Design notes (one line each):**

- **SQLite over Postgres** — the DB is a single file that commits to the repo, so the deployed app has zero connection secrets and zero cold-start failures. For a read-only single-user dashboard this is strictly simpler with no real downside.
- **`app.py` at repo root** — Streamlit Community Cloud defaults to it; putting it in `src/` creates deploy friction for no benefit.
- **`queries.py` separate from `app.py`** — makes the SQL skimmable in one file, which is precisely the thing the SQL certificate is meant to demonstrate.
- **Notebook kept, but not load-bearing** — it shows exploratory work (Data Analysis with Python cert) while the pipeline shows production thinking. Neither imports the other.

---

## 5. Build steps

Ten steps, ordered. Each is finishable in one sitting. Do not start a step before its predecessor's acceptance criterion passes.

---

### Step 1 — Repo skeleton and dependencies
**Files:** `README.md` (stub), `requirements.txt`, `.gitignore`, empty `src/` package, `data/raw/`

Create the GitHub repo and the tree above. Pin: `pandas`, `openpyxl`, `streamlit`, `plotly`, `pytest`. `.gitignore` must exclude `data/raw/` and `*.xlsx` but explicitly **not** `data/h1b.db`.

**Done when:** `pip install -r requirements.txt` succeeds in a fresh venv and `git status` shows a clean tree with no `.xlsx` files staged.

---

### Step 2 — Manual data acquisition
**Files:** `data/raw/`, `README.md` (data-source section)

Download LCA disclosure files for FY2024, FY2025, and FY2026 Q1–Q2 from the DOL OFLC performance page. These are `.xlsx`, roughly 100–200 MB each. Record each file's exact URL, download date, and row count in the README — provenance is a portfolio signal, not paperwork.

**Done when:** three or more files sit in `data/raw/`, and the README lists each with its source URL and row count.

---

### Step 3 — Exploration notebook
**Files:** `notebooks/01_exploration.ipynb`

Open one year's file in pandas. Answer, in the notebook, with output visible:

- How many distinct values does `WAGE_UNIT_OF_PAY` take, and in what proportion?
- How many rows have `WAGE_RATE_OF_PAY_FROM` of 0, null, or absurd (< $10k or > $2M annualized)?
- How badly is `EMPLOYER_NAME` fragmented? (Count variants of one large employer — trailing punctuation, `INC` vs `INC.`, case.)
- Do the FY2024, FY2025, and FY2026 files share identical column names? **They often do not** — DOL renames columns between years.

This step exists to find the mess *before* writing the cleaner. Do not skip it and do not write `clean.py` first.

**Done when:** the notebook runs top to bottom, and there is a written markdown cell listing every data problem found. That list becomes Step 4's spec.

---

### Step 4 — Cleaning module
**Files:** `src/clean.py`, `src/ingest.py`

`ingest.py` gets one function: `read_raw(path) -> DataFrame`, which reads the xlsx and harmonizes column names across fiscal years to a single canonical set (this is why Step 3 checked).

`clean.py` implements, as separate small functions:

- `to_wage(values, column)` — parse a wage column to `float64`; raise on a non-blank value that is not a number
- `repair_units(values, unit, column)` — annualization multiplier, with wrong unit labels repaired (hourly × 2080, weekly × 52, bi-weekly × 26, monthly × 12)
- `annualize(df)` / `annualize_prevailing(df)` — apply that to the offered wage band and to the prevailing wage
- `filter_status(df)` — keep `Certified` and `Certified - Withdrawn` only (A4)
- `is_tech(soc_codes)` — SOC major group 15 plus 11-3021, per A5
- `normalize_employer(name) -> str` — uppercase, strip punctuation, collapse whitespace, strip trailing corporate suffixes
- `normalize_city(city)` / `normalize_state(state)` — title-case city, two-letter uppercase state
- `flag_outliers(wages)` — mark rather than delete rows outside $10k–$2M annualized; the dashboard filters them, but they stay auditable
- `stage_counts(df, tech_only=True, rows_read=None, cleaned=None)` — rows surviving each stage, reconciled against `clean()`; pass `cleaned` if you already hold it, as `load.py` will

Every function is pure: Series or DataFrame in, Series or DataFrame out, no I/O, no globals.

> **Corrected after the build.** This list originally named `normalize_wage`
> and `filter_tech_soc`, neither of which was written — Step 3's findings split
> the wage work across `to_wage`, `repair_units`, and `annualize`, and the SOC
> filter became a predicate rather than a filter. The stale names went on to
> mislead four reviews, so the list now matches the code.

**Done when:** running the pipeline over one raw file produces a DataFrame where the wage unit is entirely annual, no nulls remain in the wage column for non-flagged rows, and the row count drop from raw to clean is explainable — `stage_counts` returns it, and the caller prints it, since nothing in `clean.py` does I/O.

---

### Step 5 — Tests for the cleaner
**Files:** `tests/test_clean.py`, `tests/test_pipeline_numbers.py`, `conftest.py`

Cover at minimum: hourly→annual conversion, monthly→annual, a null wage, a zero wage, a $5M outlier, `"MICROSOFT CORPORATION"` and `"Microsoft Corp."` normalizing to the same string, and a lowercase city with a lowercase state.

Small step, disproportionate portfolio value — most junior projects have no tests at all, and this is the cheapest way to look senior.

A second file, `tests/test_pipeline_numbers.py`, asserts the figures the README quotes against the real filings. Fixtures cannot catch a Parquet cache built from a different set of source files: every rule still passes and every headline number quietly changes. Marked `slow`, and skipped when the data is absent so a fresh clone still gets a fast green suite.

`conftest.py` at the repo root puts that root on `sys.path` — without it the suite passes under `python -m pytest` and fails under bare `pytest`.

**Done when:** bare `pytest` passes with 8+ tests, every branch of the wage path (`to_wage`, `repair_units`, `annualize`, `annualize_prevailing`, `flag_outliers`) is exercised, and reverting any single cleaning rule turns the suite red.

---

### Step 6 — Database schema and loader
**Files:** `src/load.py`, produces `data/h1b.db`

Six-table normalized schema (see §6). `load.py` creates tables, inserts cleaned data, creates indexes, and is **idempotent** — it builds into a scratch file and renames that over `h1b.db` only once the load is complete, so re-running never duplicates rows and a failed run leaves the previous database intact.

After loading, run `VACUUM` to shrink the file. Then check size: **the file must stay under 100 MB, which is GitHub's hard limit for a single file.** There is no negotiating with it and no warning before the push is rejected.

**Done when:** `python -m src.load` builds `data/h1b.db` from scratch in one command, `SELECT COUNT(*) FROM filings` matches the cleaned row count, and running it twice produces an identical file size.

---

### Step 7 — Query layer
**Files:** `src/queries.py`

One named function per dashboard question, each returning a DataFrame. Minimum set:

- `salary_percentiles(job_title, city, state, year)` — p25/p50/p75 for a filtered slice
- `top_employers(job_title, city, limit)` — employers by filing count, with median wage
- `salary_by_city(job_title, min_filings)` — for the city comparison chart
- `salary_trend(job_title, city)` — median by fiscal year
- `title_search(prefix)` — autocomplete backing for the title picker

Write real SQL here — `GROUP BY`, `HAVING`, `JOIN`, and at least one window function (`salary_trend` with a `LAG` for year-over-year delta is the natural fit). Parameterize every query with `?` placeholders; never f-string user input into SQL. This file is the SQL certificate's exhibit — make it readable.

> **Corrected after Step 7 was built.** Two things this section assumed turned out not to hold.
>
> **Query the base tables, not `v_filings`.** §6 said the opposite, and it was wrong: matching a job title case-insensitively against the joined view takes 156 ms, where resolving it through `titles` and filtering on the indexed `title_id` takes 7 ms. The view stays, for reading rows by hand. The explicit `JOIN`s are also what this step asked to demonstrate.
>
> **Titles must match case-insensitively.** 3,587 filings say `Data Analyst` and 777 say `DATA ANALYST`; an exact match loses 17% of them.
>
> **The unfiltered case cannot meet the 1-second bar, and no index fixes it.** Ranking all 850,321 rows costs 0.5–1.5 s because SQLite sorts for a window function whether or not an index could supply the order — four covering indexes on `annual_wage` were measured at **+58 MB for no improvement**. With any job title selected every function answers in 8–120 ms, so `queries.DEFAULT_JOB_TITLE` exists and Step 8's sidebar must open with it set.

**Done when:** every function runs against `h1b.db` from a plain Python REPL and returns non-empty results for `("Data Analyst", "Austin", "TX")`, and no query with a job title selected takes over 1 second.

---

### Step 8 — Streamlit dashboard
**Files:** `app.py`

Layout:

- **Sidebar:** job title search, city/state, fiscal year, minimum-filings threshold, an "exclude flagged outliers" toggle (default on)
- **Top row:** three metric cards — median, p25, p75 salary for the current filter, with filing count
- **Main:** a salary distribution histogram, a top-employers table, a year-over-year trend line
- **Footer:** data source, download date, and one honest sentence on what H-1B data does and does not represent

Wrap every query call in `@st.cache_data`. Handle the empty-result case explicitly with a readable message, not a stack trace — this is the single most common way these dashboards embarrass their author in front of a recruiter.

**Done when:** `streamlit run app.py` works locally, every filter combination either renders or shows a clean "no data" message, and no combination raises an exception.

---

### Step 9 — Deploy
**Files:** none new; possibly `.streamlit/config.toml`

Push to GitHub with `h1b.db` committed. Connect the repo to Streamlit Community Cloud, point it at `app.py`, deploy.

**Done when:** the public URL loads in a private browser window on a machine that has never seen the project, and a full filter interaction works end to end.

---

### Step 10 — README, the actual deliverable
**Files:** `README.md`

This is the portfolio piece. Reviewers read the README and maybe skim two files; most never run the code.

Must contain, in order:

1. One-sentence description and the **live demo link at the very top**
2. A screenshot or GIF of the dashboard
3. Data source with URL, date range, and row counts
4. **A "Data cleaning decisions" section** — the wage-unit normalization, the status filter, the outlier threshold, the employer normalization. State each decision and its rationale. This section is what separates this from every other Streamlit project on GitHub.
5. Architecture diagram (the ASCII one from §4 is fine)
6. How to run locally, in three commands
7. **A "Limitations" section** — H-1B wages skew toward employers who sponsor, offered wage is not total comp, and the employer normalization is heuristic and imperfect. Naming your own project's weaknesses reads as competence, not weakness.

**Done when:** someone who has never seen the project can read the README, understand what it does and what the data means, and reach the live demo in one click.

---

## 6. Data model

SQLite. Six tables, normalized enough to justify joins without being academic about it.

> **Corrected after Step 6 was built.** This section originally specified three
> tables with `job_title` stored inline and wages as `REAL`. That schema was
> built and measured at **148 MB** — above GitHub's 100 MB hard limit for a
> single file, so a repo containing it cannot be pushed at all. Step 6's own
> text said "if `h1b.db` exceeds ~200 MB, drop unused source columns", which is
> a threshold above the one that actually blocks you. The schema below is what
> `src/load.py` builds: 78 MB with all 850,321 filings and no columns dropped.
> `queries.py` filters the base tables and uses `v_filings` only for row-level inspection; see the note in Step 7.

```sql
CREATE TABLE employers (
    employer_id     INTEGER PRIMARY KEY,
    employer_name   TEXT NOT NULL UNIQUE,   -- normalized form
    raw_name_sample TEXT                    -- one original spelling, for auditability
);

CREATE TABLE occupations (
    soc_id    INTEGER PRIMARY KEY,
    soc_code  TEXT NOT NULL UNIQUE,         -- e.g. '15-2051'
    soc_title TEXT NOT NULL                 -- e.g. 'Data Scientists'
);

-- Titles stay raw; storing each of the 123,990 distinct ones once instead of
-- 850,321 times is a storage decision, not a data one. Saves 29 MB.
CREATE TABLE titles (
    title_id  INTEGER PRIMARY KEY,
    job_title TEXT NOT NULL UNIQUE
);

CREATE TABLE locations (
    location_id    INTEGER PRIMARY KEY,
    worksite_city  TEXT NOT NULL,
    worksite_state TEXT NOT NULL,           -- 2-letter
    UNIQUE (worksite_city, worksite_state)
);

CREATE TABLE visa_classes (                 -- H-1B, E-3, H-1B1; all loaded
    visa_class_id INTEGER PRIMARY KEY,
    visa_class    TEXT NOT NULL UNIQUE
);

CREATE TABLE filings (
    case_serial     INTEGER PRIMARY KEY,    -- the digits of 'I-200-25001-000001'
    case_prefix     INTEGER NOT NULL,       -- the 200 in 'I-200-'
    employer_id     INTEGER NOT NULL REFERENCES employers(employer_id),
    soc_id          INTEGER NOT NULL REFERENCES occupations(soc_id),
    title_id        INTEGER NOT NULL REFERENCES titles(title_id),
    location_id     INTEGER REFERENCES locations(location_id),
    visa_class_id   INTEGER NOT NULL REFERENCES visa_classes(visa_class_id),
    annual_wage     INTEGER,                -- whole USD/year, band midpoint
    annual_from     INTEGER,
    annual_to       INTEGER,                -- NULL when the filing gave no band
    prevailing_wage INTEGER,
    fiscal_year     INTEGER NOT NULL,
    full_time       INTEGER NOT NULL,       -- 0/1
    withdrawn       INTEGER NOT NULL,       -- 1 = 'Certified - Withdrawn'
    is_outlier      INTEGER NOT NULL DEFAULT 0,
    pw_outlier      INTEGER NOT NULL DEFAULT 0,
    unit_repaired   INTEGER NOT NULL DEFAULT 0,
    pw_repaired     INTEGER NOT NULL DEFAULT 0
);

-- Only the two columns queries filter on. Each index costs ~9 MB at this row
-- count; fiscal_year (3 distinct values) and soc_id (63) are too
-- low-cardinality to beat a scan, and titles.job_title is already indexed by
-- its UNIQUE constraint, which is what title_search uses.
CREATE INDEX idx_filings_title    ON filings(title_id);
CREATE INDEX idx_filings_location ON filings(location_id);

-- Everything joined, case number reassembled. For reading rows by hand;
-- queries.py filters the base tables directly, which is 20x faster.
CREATE VIEW v_filings AS ...;               -- see src/load.py for the body
```

**Key types and rules:**

- `annual_wage` is always whole USD/year; `NULL` if the source was unparseable. Rows with `NULL` wages load but are excluded from every aggregate.
- `job_title` stays raw. It is what users search on and normalizing it destroys signal; `soc_title` provides the clean grouping.
- `is_outlier = 1` for annualized wages under $10,000 or over $2,000,000. Never deleted, only filtered. `pw_outlier` is the same test on the prevailing wage, flagged separately because a filing can pair a sensible offer with a nonsense prevailing wage.
- `case_serial` is the natural key. Using it as the rowid enforces uniqueness without a second index, which a `case_number TEXT UNIQUE` column costs 12.8 MB for.
- The loader writes to a scratch file and renames it into place, so a failed run leaves the previous database intact rather than replacing it with a valid-looking empty one.

**Query-layer function signatures:**

```python
salary_percentiles(job_title: str|None, city: str|None, state: str|None,
                   fiscal_year: int|None, include_outliers: bool = False) -> DataFrame
                   # cols: p25, p50, p75, n_filings

top_employers(job_title: str|None, city: str|None, limit: int = 20) -> DataFrame
                   # cols: employer_name, n_filings, median_wage

salary_by_city(job_title: str, min_filings: int = 10) -> DataFrame
                   # cols: worksite_city, worksite_state, median_wage, n_filings

salary_trend(job_title: str, city: str|None) -> DataFrame
                   # cols: fiscal_year, median_wage, n_filings, yoy_pct_change

title_search(prefix: str, limit: int = 25) -> list[str]
```

---

## 7. Risks

| Risk | Likelihood | Mitigation / cheaper fallback |
|---|---|---|
| **Column names differ across fiscal years** | High — DOL does this routinely | Step 3 catches it before any code depends on a name. Fallback: build a `COLUMN_ALIASES` dict in `ingest.py` mapping each year's names to canonical ones. Budget an extra evening. |
| **`h1b.db` too large for comfortable git** | Medium | Prune columns first, then drop to two fiscal years, then FY2026 only. Do **not** reach for Git LFS — it breaks Streamlit Cloud deploys. |
| **Reading 200 MB `.xlsx` files is very slow or OOMs** | Medium-high | Convert each raw file to Parquet once in `ingest.py` and read Parquet thereafter. Cuts load time by roughly 10x. |
| **Employer normalization over-merges** (two genuinely distinct "TECH SOLUTIONS LLC") | Medium | Keep `raw_name_sample`, cap normalization at punctuation/case/suffix stripping. Explicitly do not attempt fuzzy matching in v1 — say so in the README's Limitations. |
| **Streamlit Cloud free tier sleeps or resource-limits** | Medium | Expected on free tier. Note "may take ~30s to wake" next to the demo link. Fallback: Hugging Face Spaces, same code. |
| **Project reads as "another Streamlit dashboard"** | Medium — the real portfolio risk | Countered by Step 10's cleaning-decisions and limitations sections plus Step 5's tests. If time runs short, cut a chart, never the README. |
| **Scope creep into salary prediction** | High, it's a fun idea | It's in §3 for a reason. Put it in `FUTURE.md` and ship v1 first. |

---

## 8. Open questions

Answer before or during the build — none block Step 1.

1. **Repo name.** `h1b-salary-explorer` is the working slug. Anything clearer is fine; it appears in the demo URL, so it is worth ten seconds of thought.
2. **Fiscal year depth (A3).** Three years is the assumption. If `h1b.db` gets uncomfortable at Step 6, is dropping to FY2025+FY2026 acceptable? (Recommendation: yes — the trend chart gets thin but nothing else suffers.)
3. **Tech-only filter (A5).** Confirm you want tech-only. Keeping all occupations makes the dataset ~3x larger and the story muddier, but broadens who finds the demo interesting.
4. **Is there a specific role being applied for?** If so, the dashboard's default view should land on that job title and city. Small change, real effect on a reviewer.
5. **Notebook in the repo — yes or no?** It demonstrates the analysis certificate but adds clutter. Recommendation: keep it, in `notebooks/`, linked once from the README as "how the cleaning rules were derived."

---

## Handoff

The coder agent should start at Step 1 and work in order. Steps 3 and 4 are where the real thinking is — Step 3's findings are the input to Step 4's spec, so do not let a coder skip ahead to `clean.py` before the notebook has run.

If Step 3 reveals the data is materially different from what §6 assumes, stop and revise this plan rather than patching around it in code.
