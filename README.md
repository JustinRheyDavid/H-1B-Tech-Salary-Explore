# H-1B Tech Salary Explorer

What tech jobs in the US actually pay, based on ~1.8M salary figures that
employers filed with the Department of Labor.

**Live demo:** _not deployed yet — see Step 9 of the build plan._

---

## Why this data

Most salary sites report what people say they earn. This reports what
employers legally committed to pay, on federal forms, under penalty of
perjury. Every figure here comes from a Labor Condition Application (LCA)
filed with the DOL Office of Foreign Labor Certification.

It is public data, exact rather than banded, and genuinely messy — mixed
pay units, inconsistent employer names, and column headers that change
between fiscal years. Cleaning it is most of the work.

## Status

Build in progress. Step 1 of 10 complete — see
[`docs/plans/h1b-salary-explorer.md`](docs/plans/h1b-salary-explorer.md)
for the full plan.

- [x] 1. Repo skeleton and dependencies
- [ ] 2. Data acquisition
- [ ] 3. Exploration notebook
- [ ] 4. Cleaning module
- [ ] 5. Tests
- [ ] 6. Database schema and loader
- [ ] 7. Query layer
- [ ] 8. Streamlit dashboard
- [ ] 9. Deploy
- [ ] 10. Documentation

## Data sources

Populated in Step 2. Each file will be listed with its source URL,
download date, and row count.

| Fiscal year | File | Source | Downloaded | Rows |
|---|---|---|---|---|
| _pending_ | | | | |

Raw files come from the [DOL OFLC performance data page](https://www.dol.gov/agencies/eta/foreign-labor/performance).

## Running locally

```bash
git clone https://github.com/JustinRheyDavid/H-1B-Tech-Salary-Explore.git
cd H-1B-Tech-Salary-Explore
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Requires Python 3.11 or newer.

## Layout

```
├── app.py             # Streamlit entry point            (Step 8)
├── src/
│   ├── ingest.py      # read raw DOL files               (Step 4)
│   ├── clean.py       # all normalization decisions      (Step 4)
│   ├── load.py        # schema + database build          (Step 6)
│   └── queries.py     # every SQL query the app runs     (Step 7)
├── notebooks/         # exploratory analysis             (Step 3)
├── tests/             # cleaning logic tests             (Step 5)
└── data/
    ├── raw/           # downloaded .xlsx (gitignored)
    └── h1b.db         # built SQLite database (committed)
```

## License

Source data is US federal government work and in the public domain.
Code in this repository is MIT licensed.
