"""The numbers this project publishes, checked against the real filings.

``test_clean.py`` proves each rule behaves correctly on data written to
exercise it. This file proves the rules together produce the figures the
README and the dashboard actually quote. A fixture suite cannot catch a
Parquet cache built from a different set of source files: every rule still
passes, and every headline number quietly changes.

Skipped unless the cache is already built, so a fresh clone still gets a fast
green suite. Marked ``slow`` — about 14 seconds and 2.7 GB of peak memory — so
``pytest -m "not slow"`` skips it even when the data is present.

Every constant below was measured on 2026-08-09 from the nine files named in
:data:`SOURCE_FILES`. ``test_the_documented_source_files_are_what_is_loaded``
checks those names before anything else runs, so a failure elsewhere in this
module means the *cleaner* changed, not the inputs — debug it rather than
re-deriving the constants. Only when the filename check itself fails are these
numbers expected to move, and then every one of them must be measured again.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import clean, ingest

DATA = Path(__file__).resolve().parent.parent / "data"
RAW, INTERIM = DATA / "raw", DATA / "interim"

# The exact nine files every constant below was measured from. "Dislclosure"
# is DOL's misspelling, not a typo here.
SOURCE_FILES = frozenset(
    [f"LCA_Disclosure_Data_FY2024_Q{q}.xlsx" for q in (1, 2, 3, 4)]
    + [f"LCA_Disclosure_Data_FY2025_Q{q}.xlsx" for q in (1, 2, 3, 4)]
    + ["LCA_Dislclosure_Data_FY2026_Q2.xlsx"]
)

# Measured 2026-08-09. See the module docstring before changing any of these.
ROWS_READ = 1_367_976  # after blank padding is dropped on read
UNIQUE_FILINGS = 1_347_103  # after deduplicating on case number
CERTIFIED = 1_315_799  # after the case-status filter
TECH_FILINGS = 850_321  # after the SOC filter
MEDIAN_TECH_WAGE = 130_000.0
SOFTWARE_DEVELOPERS = 410_142  # excluding outliers
SOFTWARE_DEVELOPER_MEDIAN = 139_774.46
OFFERED_UNIT_REPAIRS = 1_851  # among certified filings
PREVAILING_UNIT_REPAIRS = 9
MAX_PREVAILING_WAGE = 13_497_100.0  # wrong at source, unfixable, flagged
FISCAL_YEAR_BUCKETS = {2024: 353_308, 2025: 364_470, 2026: 132_543}
FILINGS_FLOORED = 3  # no decision date, received before FY2024


def _why_skip() -> str:
    if not RAW.is_dir() or not list(RAW.glob("*.xlsx")):
        return "no source files in data/raw — see README, Data sources"
    missing = [
        p.name
        for p in ingest.source_files(RAW)
        if not (INTERIM / f"{p.stem}.parquet").is_file()
    ]
    if missing:
        return (
            f"Parquet cache missing for {len(missing)} source file(s); "
            "run the pipeline once to build data/interim"
        )
    return ""


_SKIP = _why_skip()
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "cache present"),
]


@pytest.fixture(scope="module")
def raw():
    return ingest.load_all(RAW, INTERIM, clean.SOURCE_COLUMNS)


@pytest.fixture(scope="module")
def cleaned(raw):
    return clean.clean(raw)


def test_the_documented_source_files_are_what_is_loaded():
    """Guards every other constant here. Fails first, and says which file moved.

    Counting the files is not enough — swap one quarter for another and the
    count is still nine while every number below is measuring something else.
    """
    found = {p.name for p in ingest.source_files(RAW)}
    assert found == SOURCE_FILES, (
        "the constants in this module were measured on a different set of "
        f"files. Unexpected: {sorted(found - SOURCE_FILES)}. "
        f"Missing: {sorted(SOURCE_FILES - found)}. Every number here must be "
        "re-measured before it can be trusted again."
    )


def test_the_row_funnel_matches_the_documented_counts(raw, cleaned):
    counts = clean.stage_counts(raw, rows_read=ROWS_READ, cleaned=cleaned)
    assert counts["unique filings"] == UNIQUE_FILINGS
    assert counts["duplicate cases"] == ROWS_READ - UNIQUE_FILINGS
    assert counts["not certified"] == UNIQUE_FILINGS - CERTIFIED
    assert counts["rows out"] == TECH_FILINGS


def test_rows_read_matches_the_source_files():
    total = sum(
        len(ingest.load_raw(p, INTERIM, ["CASE_NUMBER"]))
        for p in ingest.source_files(RAW)
    )
    assert total == ROWS_READ


def test_median_tech_wage(cleaned):
    assert cleaned["annual_wage"].median() == MEDIAN_TECH_WAGE


def test_software_developers(cleaned):
    devs = cleaned[
        cleaned["soc_title"].eq("Software Developers") & ~cleaned["is_outlier"]
    ]
    assert len(devs) == SOFTWARE_DEVELOPERS
    assert devs["annual_wage"].median() == pytest.approx(
        SOFTWARE_DEVELOPER_MEDIAN, abs=0.01
    )


def test_unit_repairs(raw):
    _, _, offered = clean.annualize(clean.filter_status(raw))
    _, prevailing = clean.annualize_prevailing(clean.filter_status(raw))
    assert int(offered.sum()) == OFFERED_UNIT_REPAIRS
    assert int(prevailing.sum()) == PREVAILING_UNIT_REPAIRS


def test_no_prevailing_wage_escapes_repair_and_flagging(cleaned):
    """$360,056,320 reached this column before the repair rule was applied."""
    assert cleaned["prevailing_wage"].max() == MAX_PREVAILING_WAGE
    over = cleaned["prevailing_wage"] > clean.OUTLIER_HI
    assert (cleaned.loc[over, "pw_outlier"]).all()


def test_every_row_has_a_fiscal_year(cleaned):
    """The schema declares fiscal_year NOT NULL; 1,427 rows used to be null."""
    assert cleaned["fiscal_year"].isna().sum() == 0
    assert dict(cleaned["fiscal_year"].value_counts().sort_index()) == (
        FISCAL_YEAR_BUCKETS
    )


def test_the_fiscal_year_floor_catches_the_three_pre_fy2024_filings(raw):
    """Invisible in ``cleaned``: all three are non-tech, so the tech-only
    buckets look identical with the floor removed. Pin them directly.
    """
    certified = clean.filter_status(raw)
    received = pd.to_datetime(certified["RECEIVED_DATE"], errors="coerce")
    received_fy = received.dt.year + (received.dt.month >= 10)
    no_decision = pd.to_datetime(certified["DECISION_DATE"], errors="coerce").isna()

    floored = no_decision & (received_fy < clean.EARLIEST_FISCAL_YEAR)
    assert int(floored.sum()) == FILINGS_FLOORED

    years = clean.fiscal_year(certified["DECISION_DATE"], certified["RECEIVED_DATE"])
    assert (years[floored] == clean.EARLIEST_FISCAL_YEAR).all()
    assert years.min() == clean.EARLIEST_FISCAL_YEAR


def test_no_flag_is_null(cleaned):
    """A null flag is dropped by a boolean filter without raising."""
    flags = ["is_outlier", "pw_outlier", "unit_repaired", "pw_repaired"]
    assert not cleaned[flags].isna().any().any()
    for flag in flags:
        assert cleaned[flag].dtype == bool, flag


def test_wages_are_annual_and_bounded(cleaned):
    """After repair, every unflagged wage is a believable annual figure."""
    usable = cleaned.loc[~cleaned["is_outlier"], "annual_wage"]
    assert usable.notna().all()
    assert usable.between(clean.OUTLIER_LO, clean.OUTLIER_HI).all()
