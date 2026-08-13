# Data sources

Nine quarterly files from the [DOL OFLC performance data page](https://www.dol.gov/agencies/eta/foreign-labor/performance),
under **Disclosure Data → LCA Programs (H-1B, H-1B1, E-3)**. Downloaded
2026-08-06.

Together they cover **October 2023 through March 2026** — ten consecutive
quarters, no gaps. Periods below are `DECISION_DATE` ranges read from the files
themselves rather than inferred from the filenames.

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

Record layouts (`LCA_Record_Layout_FY2025_Q4.pdf`,
`LCA_Record_Layout_FY2026_Q2.pdf`) are the official column dictionaries and are
kept alongside the data.

## Rebuilding the database

The source spreadsheets are 850 MB and gitignored. To rebuild `data/h1b.db`
from scratch, download all nine into `data/raw/` and run:

```bash
python -m src.load
```

The first run converts each spreadsheet to a Parquet cache in `data/interim/`
— about 15 minutes — and later runs take seconds. Delete `data/interim/` if you
change which source files are present: a cache built from a different set
produces different numbers with nothing to indicate it.

Do not keep the repository inside iCloud Drive, OneDrive or Dropbox. A sync
client that uploads and evicts a 78 MB database will replace it with a stub,
and `git` inside a synced folder is its own class of problem.

## Known quirks in the source files

Found by inspecting all nine files before writing any cleaning code. Each one
breaks an assumption a reasonable person would make.

1. **73% of all rows are blank padding.** 3,610,511 of 4,978,487 rows are
   entirely empty. The padding is wildly inconsistent — four files have none at
   all, while `FY2025_Q1` has 935,457 blank rows against 107,414 real ones.
   `pd.read_excel(...).shape` reports the padded count, so any row count or
   average computed before dropping blanks is wrong.
2. **Every file has a different sheet name** — `Q1`…`Q4`,
   `LCA_Disclosure_Data_FY2025_Q1`…`_Q4`, and `Sheet1`. Nine files, nine names.
   Select the sheet by index, never by name.
3. **The column set changes mid-fiscal-year.** FY2024 (all quarters) and
   `FY2025_Q1` have 97 columns; `FY2025_Q2` onward have 98, the addition being
   `LAWFIRM_BUSINESS_FEIN`. The change lands between Q1 and Q2 of FY2025, *not*
   at a fiscal year boundary — so keying the schema off the year in the
   filename would be wrong.
4. **DOL misspelled one filename** — `LCA_Dislclosure_Data_FY2026_Q2.xlsx`
   ("Dislclosure"). Match files by glob, not by literal name.
5. **20,873 case numbers appear in two files each.** Every duplicate spans a
   quarter boundary and none are repeated within a single file — a case decided
   near the cutoff gets published in both quarters. Deduplicate on
   `CASE_NUMBER`, keeping the later decision.
6. **FY2026_Q2 is cumulative; the rest are not.** It covers two quarters
   (Oct 2025–Mar 2026) while every other file covers one. Naming alone does not
   tell you this.
7. **A third of filings give a wage *band*, not a figure.**
   `WAGE_RATE_OF_PAY_TO` is populated on 32% of rows, with a median spread of
   22%. Reading `WAGE_RATE_OF_PAY_FROM` alone understates those salaries by
   13.8%. This project reports the midpoint and keeps both columns.
8. **3,221 filings use the wrong wage unit.** Annual salaries were filed
   against `Hour`, `Week`, `Bi-Weekly`, and `Month`. Left uncorrected the
   maximum annualized wage is $1.47 billion and the mean is $428,938 against a
   median of $118,248. After repair the mean is $130,848.
9. **An Excel escape hides in two columns.** `SOC_CODE` and `JOB_TITLE` arrive
   as `="15-1252.00"` on 130,000 rows each. A prefix filter that does not strip
   it silently drops 9.5% of tech filings with no error. Strip with
   `^="(.*)"$`, anchored at both ends — an unanchored alternation corrupts a
   legitimate title ending in a quote.
10. **Not every row is H-1B.** `VISA_CLASS` also contains E-3 Australian, H-1B1
    Chile, and H-1B1 Singapore — roughly 3% of rows. All are loaded: they are
    filed on the same form under the same wage rules, and the `visa_class`
    column lets you exclude them if you disagree.
11. **`PREVAILING_WAGE` has the same unit defect, and it is worse.** Nine
    certified filings carry an annual figure labelled `Week` or `Hour`, putting
    the maximum prevailing wage at $360,056,320. Four of them pair it with a
    perfectly ordinary offered wage, so any outlier check based on the offered
    wage alone passes them through. The two columns are repaired and flagged
    independently for that reason. Five filings survive repair with a figure
    implausible under every unit — wrong at source, flagged rather than fixed.
12. **2,216 filings have no `DECISION_DATE`.** All of them have a
    `RECEIVED_DATE`, which this project falls back to so the row keeps a fiscal
    year instead of dropping out of every trend chart. For those 0.16% of rows
    the year is when the filing was received, not when it was decided. Three
    were received in FY2023, a year these files do not otherwise cover, so the
    fallback is floored at FY2024 — a case published in this data was decided no
    earlier than that, and three rows are not a trend line. That floor is
    `clean.EARLIEST_FISCAL_YEAR`; **change it if you swap the source files for a
    different range of years.**
13. **The database has to fit in 100 MB.** GitHub refuses any file above that,
    and `data/h1b.db` is committed so the deployed dashboard needs no
    credentials. The planned three-table schema, with `job_title` stored inline
    and wages as `REAL`, measured 148 MB — unpushable. Lookup tables for job
    title, employer, occupation, location and visa class; integer wages; the
    case-number serial used as the rowid; and indexes only on the two columns
    queries filter on bring it to 78 MB. No rows were dropped to get there —
    all 850,321 filings are loaded, and the `v_filings` view joins it back
    together with the case number reassembled.
