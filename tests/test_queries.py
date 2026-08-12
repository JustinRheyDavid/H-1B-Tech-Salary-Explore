"""Tests for :mod:`src.queries`.

Each test builds a small database with wages chosen so the right answer can be
worked out by hand. Percentiles are the reason: SQLite has no
``PERCENTILE_CONT``, so the module computes them itself, and a test that only
checks "some number came back" would not notice the arithmetic being wrong.

The figures from the real 850,321-row database are checked in
``test_pipeline_numbers.py``; nothing here needs the source data.
"""

from __future__ import annotations

import numpy as np
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
    """100k..800k, so the targets land between rows and must interpolate.

    p50 = 450,000 rather than 400,000 is the point: taking the lower of the two
    middle rows biases every even-sized group downward.
    """
    row = queries.salary_percentiles(db=eight_wages).iloc[0]
    assert (row.p25, row.p50, row.p75, row.n_filings) == (275_000, 450_000, 625_000, 8)


@pytest.mark.parametrize(
    "wages",
    [
        [100_000.0, 200_000.0],
        [100_000.0, 200_000.0, 300_000.0],
        [100_000.0, 200_000.0, 300_000.0, 400_000.0],
        [90_000.0, 90_000.0, 100_000.0, 175_000.0, 175_000.0, 200_000.0],
        [float(w) for w in (61_000, 83_500, 97_250, 120_000, 155_900)],
    ],
)
def test_all_three_percentiles_agree_with_pandas(tmp_path, wages):
    """The numbers people check against. They have to be the ones they get.

    pandas, numpy and Excel all interpolate linearly, and so does Azure SQL's
    PERCENTILE_CONT, which Phase 2 ports these to.
    """
    db = database(tmp_path, WAGE_RATE_OF_PAY_FROM=list(wages))
    row = queries.salary_percentiles(db=db).iloc[0]
    series = pd.Series(wages)
    assert row.p25 == pytest.approx(series.quantile(0.25))
    assert row.p50 == pytest.approx(series.median())
    assert row.p75 == pytest.approx(series.quantile(0.75))


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


def test_fiscal_years_come_from_the_data_not_from_one_job_title(tmp_path):
    """A year with filings but none under the default title must still appear.

    Derived from ``salary_trend(DEFAULT_JOB_TITLE)`` this list silently omits
    such a year, and is empty in a database that lacks the default title —
    leaving the picker unable to select a year that is plainly there.
    """
    db = database(
        tmp_path,
        JOB_TITLE=["Bioinformatics Scientist"] * 3,
        DECISION_DATE=["2024-05-01", "2025-05-01", "2026-01-15"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 3,
    )
    assert queries.salary_trend(queries.DEFAULT_JOB_TITLE, db=db).empty
    assert queries.fiscal_years(db=db) == [2026, 2025, 2024]


def test_fiscal_years_lists_each_year_once_newest_first(tmp_path):
    """Repeats on purpose: most years have hundreds of thousands of filings."""
    db = database(
        tmp_path,
        DECISION_DATE=[
            "2026-01-15", "2024-05-01", "2025-05-01",
            "2024-06-01", "2025-06-01", "2024-07-01",
        ],
        WAGE_RATE_OF_PAY_FROM=[100_000.0] * 6,
    )
    assert queries.fiscal_years(db=db) == [2026, 2025, 2024]


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


def test_title_search_treats_none_as_no_prefix(tmp_path):
    """A picker with nothing selected hands back None, not an empty string."""
    db = database(
        tmp_path,
        JOB_TITLE=["Engineer", "Analyst"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 100_000.0],
    )
    assert sorted(queries.title_search(None, db=db)) == ["Analyst", "Engineer"]
    assert queries.title_search(None, db=db) == queries.title_search("", db=db)


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
    ("call", "kwargs"),
    [
        (queries.top_employers, {"limit": -1}),
        (queries.title_search, {"limit": -5}),
        (queries.salary_by_city, {"min_filings": -3}),
    ],
)
def test_a_negative_count_is_refused_not_reinterpreted(eight_wages, call, kwargs):
    """SQLite reads a negative LIMIT as no limit, so -1 returns everything.

    An autocomplete asking for 25 titles was handed 5,253, and nothing
    downstream would have reported it.
    """
    with pytest.raises(ValueError, match="must not be negative"):
        call(db=eight_wages, **kwargs)


@pytest.mark.parametrize(
    ("call", "kwargs"),
    [
        (queries.top_employers, {"limit": "20"}),
        (queries.top_employers, {"limit": True}),
        (queries.salary_percentiles, {"fiscal_year": "2025"}),
    ],
)
def test_a_count_that_is_not_a_whole_number_is_refused(eight_wages, call, kwargs):
    """A string fiscal_year compares against an INTEGER column and matches
    nothing, which reads as "no filings that year" rather than as a typo.

    ``True`` is rejected too: bool subclasses int, so it would mean ``1``.
    """
    with pytest.raises(TypeError, match="whole number"):
        call(db=eight_wages, **kwargs)


@pytest.mark.parametrize("value", ["no", "false", "0", "yes", 1, 0, None, []])
def test_include_outliers_must_be_a_real_boolean(eight_wages, value):
    """Every non-empty string is truthy, so "no" would turn the filter off."""
    with pytest.raises(TypeError, match="True or False"):
        queries.salary_percentiles(include_outliers=value, db=eight_wages)


@pytest.mark.parametrize(
    "kwargs",
    [{"job_title": 123}, {"city": 456}, {"state": 7}, {"job_title": ["a"]}],
)
def test_a_filter_that_is_not_text_is_refused(eight_wages, kwargs):
    """An integer compared against a TEXT column matches nothing, which reads
    as "no filings" rather than as the wrong type."""
    with pytest.raises(TypeError, match="text or None"):
        queries.salary_percentiles(db=eight_wages, **kwargs)


def test_title_search_refuses_a_prefix_that_is_not_text(eight_wages):
    with pytest.raises(TypeError, match="text or None"):
        queries.title_search(123, db=eight_wages)


def test_values_taken_straight_back_out_of_a_returned_frame_are_accepted(tmp_path):
    """Every function here returns a DataFrame, so its columns are numpy types.

    ``numpy.int64`` is not a subclass of ``int``, so a strict isinstance check
    rejects this module's own output — which is precisely what a year selector
    populated from salary_trend() would hand back.
    """
    db = database(
        tmp_path,
        DECISION_DATE=["2024-05-01", "2025-05-01"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 110_000.0],
    )
    year = queries.salary_trend(db=db)["fiscal_year"].iloc[0]
    assert type(year) is not int, "fixture no longer exercises the numpy case"

    row = queries.salary_percentiles(fiscal_year=year, db=db).iloc[0]
    assert row.n_filings == 1


@pytest.mark.parametrize("year", [np.int64(2025), np.int32(2025), 2025])
def test_a_numpy_integer_is_a_whole_number(tmp_path, year):
    db = database(
        tmp_path, DECISION_DATE=["2025-05-01"], WAGE_RATE_OF_PAY_FROM=[100_000.0]
    )
    assert queries.salary_percentiles(fiscal_year=year, db=db).iloc[0].n_filings == 1


@pytest.mark.parametrize("flag", [np.bool_(True), True])
def test_a_numpy_boolean_is_a_flag(eight_wages, flag):
    assert queries.salary_percentiles(include_outliers=flag, db=eight_wages).iloc[
        0
    ].n_filings == 8


def test_the_validators_hand_back_plain_python_types():
    """Not decoration: sqlite3 cannot bind a numpy scalar as a parameter, and
    the annotations would otherwise be untrue for anything out of a DataFrame.
    """
    assert type(queries._whole_number("limit", np.int64(20))) is int
    assert type(queries._flag("include_outliers", np.bool_(True))) is bool


@pytest.mark.parametrize("limit", [2**63, 10**20])
def test_a_count_too_large_for_sqlite_is_refused(eight_wages, limit):
    """Otherwise it fails inside pandas with the entire query in the message."""
    with pytest.raises(ValueError, match="must be at most"):
        queries.top_employers(limit=limit, db=eight_wages)


def test_the_largest_integer_sqlite_can_hold_is_still_accepted(eight_wages):
    assert len(queries.top_employers(limit=2**63 - 1, db=eight_wages)) == 1


def test_zero_is_a_legitimate_count(eight_wages):
    assert len(queries.top_employers(limit=0, db=eight_wages)) == 0
    assert queries.title_search(limit=0, db=eight_wages) == []
    assert len(queries.salary_by_city(min_filings=0, db=eight_wages)) == 1


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
