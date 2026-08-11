"""Tests for :mod:`src.queries`.

Each test builds a small database with wages chosen so the right answer can be
worked out by hand. Percentiles are the reason: SQLite has no
``PERCENTILE_CONT``, so the module computes them itself, and a test that only
checks "some number came back" would not notice the arithmetic being wrong.

The figures from the real 850,321-row database are checked in
``test_pipeline_numbers.py``; nothing here needs the source data.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import clean, load, queries

DEFAULTS: dict[str, object] = {
    "CASE_NUMBER": "I-200-25001-000001",
    "CASE_STATUS": "Certified",
    "VISA_CLASS": "H-1B",
    "DECISION_DATE": "2025-01-15",
    "RECEIVED_DATE": "2024-11-01",
    "EMPLOYER_NAME": "ACME INC",
    "JOB_TITLE": "Software Engineer",
    "SOC_CODE": "15-1252.00",
    "SOC_TITLE": "Software Developers",
    "WORKSITE_CITY": "austin",
    "WORKSITE_STATE": "tx",
    "WAGE_RATE_OF_PAY_FROM": 100_000.0,
    "WAGE_RATE_OF_PAY_TO": None,
    "WAGE_UNIT_OF_PAY": "Year",
    "PREVAILING_WAGE": 95_000.0,
    "PW_UNIT_OF_PAY": "Year",
    "FULL_TIME_POSITION": "Y",
}


def database(tmp_path, **columns: list) -> str:
    """Build a database from parallel lists, one entry per filing."""
    rows = max(len(v) for v in columns.values())
    fields = {**DEFAULTS, **columns}
    fields.setdefault(
        "CASE_NUMBER", [f"I-200-25001-{i:06d}" for i in range(1, rows + 1)]
    )
    if not isinstance(fields["CASE_NUMBER"], list):
        fields["CASE_NUMBER"] = [f"I-200-25001-{i:06d}" for i in range(1, rows + 1)]
    frame = pd.DataFrame(
        {k: v if isinstance(v, list) else [v] * rows for k, v in fields.items()}
    )
    path, _ = load.build(clean.clean(frame), tmp_path / "h1b.db")
    return str(path)


@pytest.fixture
def eight_wages(tmp_path):
    """Eight filings at 100k..800k.

    Eight, not four: with four evenly spaced wages several wrong percentile
    formulas land on the same answer as the right ones, so a smaller fixture
    would pass whatever the arithmetic said. At eight, nearest-rank gives
    p25=200k, p50=400k, p75=600k and every neighbouring formula differs.
    """
    return database(
        tmp_path, WAGE_RATE_OF_PAY_FROM=[float(i * 100_000) for i in range(1, 9)]
    )


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------


def test_percentiles_over_an_even_number_of_filings(eight_wages):
    """100k..800k. The median averages the two middle rows; p25 and p75 do not.

    450,000 rather than 400,000 is the whole point: taking the lower of the two
    middle values biases every even-sized group downward.
    """
    row = queries.salary_percentiles(db=eight_wages).iloc[0]
    assert (row.p25, row.p50, row.p75, row.n_filings) == (200_000, 450_000, 600_000, 8)


@pytest.mark.parametrize(
    "wages",
    [
        [100_000.0, 200_000.0],
        [100_000.0, 200_000.0, 300_000.0],
        [100_000.0, 200_000.0, 300_000.0, 400_000.0],
        [90_000.0, 90_000.0, 100_000.0, 175_000.0, 175_000.0, 200_000.0],
    ],
)
def test_the_median_agrees_with_pandas(tmp_path, wages):
    """The number people check against. It has to be the one they get."""
    db = database(tmp_path, WAGE_RATE_OF_PAY_FROM=list(wages))
    assert queries.salary_percentiles(db=db).iloc[0].p50 == pd.Series(wages).median()


def test_the_city_median_agrees_with_pandas(tmp_path):
    """Where the old rule actually bit: 34% of real city medians read low."""
    wages = [100_000.0, 120_000.0, 140_000.0, 190_000.0]
    db = database(tmp_path, WAGE_RATE_OF_PAY_FROM=wages)
    frame = queries.salary_by_city(min_filings=1, db=db)
    assert frame["median_wage"].iloc[0] == pd.Series(wages).median() == 130_000.0


def test_percentiles_of_a_single_filing_are_all_that_filing(tmp_path):
    db = database(tmp_path, WAGE_RATE_OF_PAY_FROM=[123_000.0])
    row = queries.salary_percentiles(db=db).iloc[0]
    assert (row.p25, row.p50, row.p75, row.n_filings) == (123_000, 123_000, 123_000, 1)


def test_an_empty_slice_returns_one_row_of_nulls_not_no_rows(eight_wages):
    """The dashboard needs something to render either way."""
    frame = queries.salary_percentiles("No Such Job", db=eight_wages)
    assert len(frame) == 1
    assert frame["n_filings"].iloc[0] == 0
    assert frame["p50"].isna().all()


def test_outliers_are_excluded_unless_asked_for(tmp_path):
    db = database(
        tmp_path, WAGE_RATE_OF_PAY_FROM=[100_000.0, 200_000.0, 9_000_000.0]
    )
    assert queries.salary_percentiles(db=db).iloc[0].n_filings == 2
    with_outliers = queries.salary_percentiles(include_outliers=True, db=db)
    assert with_outliers.iloc[0].n_filings == 3


def test_a_filing_with_no_wage_is_never_counted(tmp_path):
    db = database(tmp_path, WAGE_RATE_OF_PAY_FROM=[100_000.0, None])
    frame = queries.salary_percentiles(include_outliers=True, db=db)
    assert frame.iloc[0].n_filings == 1


@pytest.mark.parametrize("spelling", ["Data Analyst", "DATA ANALYST", "data analyst"])
def test_titles_match_whatever_case_the_employer_filed(tmp_path, spelling):
    """3,587 real filings say "Data Analyst" and 777 say "DATA ANALYST"."""
    db = database(
        tmp_path,
        JOB_TITLE=["Data Analyst", "DATA ANALYST", "data analyst"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 100_000.0, 100_000.0],
    )
    assert queries.salary_percentiles(spelling, db=db).iloc[0].n_filings == 3


def test_the_city_and_state_filters_apply(tmp_path):
    db = database(
        tmp_path,
        WORKSITE_CITY=["austin", "austin", "portland"],
        WORKSITE_STATE=["tx", "or", "or"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 3,
    )
    assert queries.salary_percentiles(city="Austin", db=db).iloc[0].n_filings == 2
    assert queries.salary_percentiles(state="OR", db=db).iloc[0].n_filings == 2
    assert (
        queries.salary_percentiles(city="austin", state="tx", db=db).iloc[0].n_filings
        == 1
    )


def test_the_fiscal_year_filter_applies(tmp_path):
    db = database(
        tmp_path,
        DECISION_DATE=["2024-05-01", "2025-05-01", "2025-06-01"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 3,
    )
    assert queries.salary_percentiles(fiscal_year=2025, db=db).iloc[0].n_filings == 2


# --------------------------------------------------------------------------
# Grouped medians
# --------------------------------------------------------------------------


def test_top_employers_orders_by_filing_count_and_reports_a_median(tmp_path):
    # Not "BIG CO": normalize_employer strips a trailing legal suffix, and CO
    # is one, so the name would arrive as "BIG" and the test would read as a
    # bug in the query rather than in the fixture.
    # Named so that ordering by count and ordering alphabetically disagree —
    # otherwise the ORDER BY could be dropped entirely and this would pass.
    db = database(
        tmp_path,
        EMPLOYER_NAME=["ZETA LABS", "ZETA LABS", "ZETA LABS", "ALPHA WORKS"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 200_000.0, 300_000.0, 500_000.0],
    )
    frame = queries.top_employers(db=db)
    assert list(frame["employer_name"]) == ["ZETA LABS", "ALPHA WORKS"]
    assert list(frame["n_filings"]) == [3, 1]
    assert list(frame["median_wage"]) == [200_000, 500_000]


def test_top_employers_respects_its_limit(tmp_path):
    db = database(
        tmp_path,
        EMPLOYER_NAME=[f"CO {i}" for i in range(5)],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 5,
    )
    assert len(queries.top_employers(limit=3, db=db)) == 3


def test_salary_by_city_drops_cities_below_the_threshold(tmp_path):
    """A city with two filings produces a median that means nothing."""
    db = database(
        tmp_path,
        WORKSITE_CITY=["austin"] * 3 + ["denton"] * 2,
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 200_000.0, 300_000.0, 900_000.0, 950_000.0],
    )
    frame = queries.salary_by_city(min_filings=3, db=db)
    assert list(frame["worksite_city"]) == ["Austin"]
    assert frame["median_wage"].iloc[0] == 200_000
    assert len(queries.salary_by_city(min_filings=2, db=db)) == 2


def test_the_same_city_in_two_states_stays_two_rows(tmp_path):
    db = database(
        tmp_path,
        WORKSITE_CITY=["portland"] * 4,
        WORKSITE_STATE=["or", "or", "me", "me"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 200_000.0, 300_000.0, 400_000.0],
    )
    frame = queries.salary_by_city(min_filings=2, db=db)
    assert sorted(frame["worksite_state"]) == ["ME", "OR"]


# --------------------------------------------------------------------------
# The trend, and its window function
# --------------------------------------------------------------------------


def test_the_trend_reports_year_over_year_change(tmp_path):
    db = database(
        tmp_path,
        DECISION_DATE=["2024-05-01", "2025-05-01"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 110_000.0],
    )
    frame = queries.salary_trend(db=db)
    assert list(frame["fiscal_year"]) == [2024, 2025]
    assert list(frame["median_wage"]) == [100_000, 110_000]
    assert frame["yoy_pct_change"].iloc[1] == 10.0


def test_the_earliest_year_has_no_year_over_year_change(tmp_path):
    """LAG behaving correctly, not missing data."""
    db = database(
        tmp_path,
        DECISION_DATE=["2024-05-01", "2025-05-01"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 110_000.0],
    )
    assert pd.isna(queries.salary_trend(db=db)["yoy_pct_change"].iloc[0])


def test_the_trend_is_ordered_by_year(tmp_path):
    db = database(
        tmp_path,
        DECISION_DATE=["2026-01-01", "2024-05-01", "2025-05-01"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 3,
    )
    years = list(queries.salary_trend(db=db)["fiscal_year"])
    assert years == sorted(years)


# --------------------------------------------------------------------------
# Title search
# --------------------------------------------------------------------------


def test_title_search_matches_a_prefix_and_orders_by_popularity(tmp_path):
    db = database(
        tmp_path,
        JOB_TITLE=["Data Engineer", "Data Engineer", "Data Analyst", "Product Manager"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 4,
    )
    assert queries.title_search("Data", db=db) == ["Data Engineer", "Data Analyst"]


def test_title_search_collapses_spellings_that_differ_only_in_case(tmp_path):
    """Otherwise the picker offers the same job twice and looks broken."""
    db = database(
        tmp_path,
        JOB_TITLE=["Data Analyst", "DATA ANALYST", "data analyst"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 3,
    )
    assert len(queries.title_search("data", db=db)) == 1


@pytest.mark.parametrize("pattern", ["%", "_ngineer", "Engineer_", "10%"])
def test_a_wildcard_typed_in_the_box_is_matched_literally(tmp_path, pattern):
    """Parameterising stops the value being syntax, not being a pattern.

    Unescaped, "%" returns the whole list and "_ngineer" matches "Engineer".
    """
    db = database(
        tmp_path,
        JOB_TITLE=["Engineer", "Engineer 2"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 100_000.0],
    )
    assert queries.title_search(pattern, db=db) == []


def test_a_literal_percent_in_a_title_is_still_findable(tmp_path):
    db = database(
        tmp_path,
        JOB_TITLE=["100% Remote Engineer", "Engineer"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 100_000.0],
    )
    assert queries.title_search("100%", db=db) == ["100% Remote Engineer"]


def test_title_search_respects_its_limit_and_can_find_nothing(tmp_path):
    db = database(
        tmp_path,
        JOB_TITLE=[f"Engineer {i}" for i in range(5)],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 5,
    )
    assert len(queries.title_search("Engineer", limit=2, db=db)) == 2
    assert queries.title_search("nothing matches this", db=db) == []


# --------------------------------------------------------------------------
# Shape, and not trusting the caller's strings
# --------------------------------------------------------------------------


def test_every_function_returns_the_columns_the_plan_specifies(eight_wages):
    assert list(queries.salary_percentiles(db=eight_wages)) == [
        "p25", "p50", "p75", "n_filings",
    ]
    assert list(queries.top_employers(db=eight_wages)) == [
        "employer_name", "n_filings", "median_wage",
    ]
    assert list(queries.salary_by_city(min_filings=1, db=eight_wages)) == [
        "worksite_city", "worksite_state", "median_wage", "n_filings",
    ]
    assert list(queries.salary_trend(db=eight_wages)) == [
        "fiscal_year", "median_wage", "n_filings", "yoy_pct_change",
    ]
    assert isinstance(queries.title_search(db=eight_wages), list)


@pytest.mark.parametrize(
    "hostile",
    ["'; DROP TABLE filings; --", "%", "_", "' OR 1=1 --", 'Data" OR "1"="1'],
)
def test_a_hostile_string_is_a_value_not_syntax(eight_wages, hostile):
    """Every argument reaches SQLite as a ? parameter, so none of this parses."""
    assert queries.salary_percentiles(hostile, db=eight_wages).iloc[0].n_filings == 0
    queries.title_search(hostile, db=eight_wages)
    queries.top_employers(hostile, db=eight_wages)

    row = queries.salary_percentiles(db=eight_wages).iloc[0]
    assert row.n_filings == 8, "the table is still there"
