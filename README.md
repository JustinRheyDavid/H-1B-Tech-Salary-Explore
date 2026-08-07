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

Build in progress. Steps 1-3 of 10 complete — see
[`docs/plans/h1b-salary-explorer.md`](docs/plans/h1b-salary-explorer.md)
for the full plan.

- [x] 1. Repo skeleton and dependencies
- [x] 2. Data acquisition
- [x] 3. Exploration notebook
- [ ] 4. Cleaning module
- [ ] 5. Tests
- [ ] 6. Database schema and loader
- [ ] 7. Query layer
- [ ] 8. Streamlit dashboard
- [ ] 9. Deploy
- [ ] 10. Documentation

## Data sources

Nine quarterly files from the [DOL OFLC performance data page](https://www.dol.gov/agencies/eta/foreign-labor/performance),
under **Disclosure Data → LCA Programs (H-1B, H-1B1, E-3)**. Downloaded 2026-08-06.

Together they cover **October 2023 through March 2026** — ten consecutive
quarters, no gaps. Periods below are `DECISION_DATE` ranges read from the
files themselves rather than inferred from the filenames.

| File | Period | Sheet rows | Blank | Real rows | Cols |
|---|---|---:|---:|---:|---:|
| `LCA_Disclosure_Data_FY2024_Q1.xlsx` | Oct–Dec 2023 | 99,692 | 0 | 99,692 | 97 |
| `LCA_Disclosure_Data_FY2024_Q2.xlsx` | Jan–Mar 2024 | 123,978 | 0 | 123,978 | 97 |
| `LCA_Disclosure_Data_FY2024_Q3.xlsx` | Apr–Jun 2024 | 694,404 | 477,934 | 216,470 | 97 |
| `LCA_Disclosure_Data_FY2024_Q4.xlsx` | Jul–Sep 2024 | 598,831 | 477,934 | 120,897 | 97 |
| `LCA_Disclosure_Data_FY2025_Q1.xlsx` | Oct–Dec 2024 | 1,042,871 | 935,457 | 107,414 | 97 |
| `LCA_Disclosure_Data_FY2025_Q2.xlsx` | Jan–Mar 2025 | 132,133 | 0 | 132,133 | 98 |
| `LCA_Disclosure_Data_FY2025_Q3.xlsx` | Apr–Jun 2025 | 683,534 | 445,109 | 238,425 | 98 |
| `LCA_Disclosure_Data_FY2025_Q4.xlsx` | Jul–Sep 2025 | 563,689 | 445,109 | 118,580 | 98 |
| `LCA_Dislclosure_Data_FY2026_Q2.xlsx` | Oct 2025–Mar 2026 | 1,039,355 | 828,968 | 210,387 | 98 |
| **Total** | | **4,978,487** | **3,610,511** | **1,367,976** | |

After deduplicating on `CASE_NUMBER`: **1,347,103 unique filings.**

Record layouts (`LCA_Record_Layout_FY2025_Q4.pdf`, `LCA_Record_Layout_FY2026_Q2.pdf`)
are the official column dictionaries and are kept alongside the data.

### Known quirks in the source files

Found by inspecting all nine files before writing any cleaning code. Each
one breaks an assumption a reasonable person would make:

1. **73% of all rows are blank padding.** 3,610,511 of 4,978,487 rows are
   entirely empty. The padding is wildly inconsistent — four files have none
   at all, while `FY2025_Q1` has 935,457 blank rows against 107,414 real
   ones. `pd.read_excel(...).shape` reports the padded count, so any row
   count or average computed before dropping blanks is wrong.
2. **Every file has a different sheet name** — `Q1`…`Q4`,
   `LCA_Disclosure_Data_FY2025_Q1`…`_Q4`, and `Sheet1`. Nine files, nine
   names. Select the sheet by index, never by name.
3. **The column set changes mid-fiscal-year.** FY2024 (all quarters) and
   `FY2025_Q1` have 97 columns; `FY2025_Q2` onward have 98, the addition
   being `LAWFIRM_BUSINESS_FEIN`. The change lands between Q1 and Q2 of
   FY2025, *not* at a fiscal year boundary — so keying the schema off the
   year in the filename would be wrong.
4. **DOL misspelled one filename** — `LCA_Dislclosure_Data_FY2026_Q2.xlsx`
   ("Dislclosure"). Match files by glob, not by literal name.
5. **20,873 case numbers appear in two files each.** Every duplicate spans
   a quarter boundary and none are repeated within a single file — a case
   decided near the cutoff gets published in both quarters. Deduplicate on
   `CASE_NUMBER`.
6. **FY2026_Q2 is cumulative; the rest are not.** It covers two quarters
   (Oct 2025–Mar 2026) while every other file covers one. Naming alone does
   not tell you this.
7. **A third of filings give a wage *band*, not a figure.**
   `WAGE_RATE_OF_PAY_TO` is populated on 32% of rows, with a median spread of
   22%. Reading `WAGE_RATE_OF_PAY_FROM` alone understates those salaries by
   13.7%. This project reports the midpoint and keeps both columns.
8. **Not every row is H-1B.** `VISA_CLASS` also contains E-3 Australian,
   H-1B1 Chile, and H-1B1 Singapore — roughly 3% of rows.

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
