"""Tests for :mod:`src.clean`.

Every test here is either a rule the plan asked for or a bug that actually
happened. The second group is the reason this file is worth its length: each
of those defects passed a reading of the code and was only caught by running
it against the real filings, so a test that pins the behaviour is the only
thing standing between the fix and its silent reversal.

Fixtures are handwritten rather than sampled from ``data/``. The nine source
files are 850 MB and gitignored, so a suite that depended on them would not
run for anyone who cloned the repo — which is most of the value of having it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import clean

# One certified, full-time, tech filing with an unremarkable annual wage.
# Every test starts here and overrides only the field it is about.
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


def raw(**overrides: object) -> pd.DataFrame:
    """A raw frame. Scalars are broadcast; pass a list for a multi-row case."""
    rows = max(
        (len(v) for v in overrides.values() if isinstance(v, list)), default=1
    )
    fields = {**DEFAULTS, **overrides}
    return pd.DataFrame(
        {k: v if isinstance(v, list) else [v] * rows for k, v in fields.items()}
    )


def one(**overrides: object) -> pd.Series:
    """The single cleaned row produced by ``raw(**overrides)``."""
    cleaned = clean.clean(raw(**overrides))
    assert len(cleaned) == 1, f"expected one row, got {len(cleaned)}"
    return cleaned.iloc[0]


# --------------------------------------------------------------------------
# Wage units — the plan's required cases
# --------------------------------------------------------------------------


def test_hourly_becomes_annual():
    assert one(WAGE_RATE_OF_PAY_FROM=60.0, WAGE_UNIT_OF_PAY="Hour")[
        "annual_wage"
    ] == 60.0 * 2080


def test_monthly_becomes_annual():
    assert one(WAGE_RATE_OF_PAY_FROM=10_000.0, WAGE_UNIT_OF_PAY="Month")[
        "annual_wage"
    ] == 120_000.0


@pytest.mark.parametrize(
    ("unit", "filed", "expected"),
    [("Week", 2_000.0, 104_000.0), ("Bi-Weekly", 4_000.0, 104_000.0)],
)
def test_remaining_units_annualize(unit, filed, expected):
    assert one(WAGE_RATE_OF_PAY_FROM=filed, WAGE_UNIT_OF_PAY=unit)[
        "annual_wage"
    ] == expected


def test_null_wage_is_flagged_never_dropped():
    """A filing with no wage still loads. The plan keeps such rows auditable."""
    row = one(WAGE_RATE_OF_PAY_FROM=None)
    assert pd.isna(row["annual_wage"])
    assert row["is_outlier"]


def test_zero_wage_is_flagged():
    row = one(WAGE_RATE_OF_PAY_FROM=0.0)
    assert row["annual_wage"] == 0.0
    assert row["is_outlier"]


def test_five_million_is_flagged_and_not_mistaken_for_a_unit_error():
    row = one(WAGE_RATE_OF_PAY_FROM=5_000_000.0)
    assert row["annual_wage"] == 5_000_000.0
    assert row["is_outlier"]
    assert not row["unit_repaired"]


# --------------------------------------------------------------------------
# Normalization — the plan's required cases
# --------------------------------------------------------------------------


def test_employer_spelling_variants_collapse():
    names = pd.Series(["MICROSOFT CORPORATION", "Microsoft Corp.", "microsoft, inc"])
    assert clean.normalize_employer(names).nunique() == 1


def test_employer_normalization_stays_conservative():
    """No fuzzy matching. These three are different companies, not one."""
    names = pd.Series(
        [
            "Cognizant Technology Solutions",
            "SparkCognizant Inc",
            "TMG HEALTH - A COGNIZANT COMPANY",
        ]
    )
    assert clean.normalize_employer(names).nunique() == 3


def test_lowercase_city_and_state_are_normalized():
    row = one(WORKSITE_CITY="austin", WORKSITE_STATE="tx")
    assert row["worksite_city"] == "Austin"
    assert row["worksite_state"] == "TX"


# --------------------------------------------------------------------------
# The Excel escape — 9.5% of tech filings vanished without it
# --------------------------------------------------------------------------


def test_escaped_soc_code_still_matches_the_tech_filter():
    assert one(SOC_CODE='="15-1252.00"')["soc_code"] == "15-1252"


def test_unescaping_keeps_quotes_inside_a_title():
    """``^="|"$`` as an alternation eats the trailing quote. Anchor both ends."""
    titles = pd.Series(['="Analyst ("FP&A")"', 'Analyst "Senior"'])
    assert clean.unescape(titles).tolist() == [
        'Analyst ("FP&A")',
        'Analyst "Senior"',
    ]


def test_soc_detail_suffix_is_truncated():
    assert clean.normalize_soc(pd.Series(["15-1252.00"]))[0] == "15-1252"


# --------------------------------------------------------------------------
# Unit repair — annual salaries filed against the wrong unit
# --------------------------------------------------------------------------


def test_annual_salary_filed_as_hourly_is_repaired():
    row = one(WAGE_RATE_OF_PAY_FROM=173_104.0, WAGE_UNIT_OF_PAY="Hour")
    assert row["annual_wage"] == 173_104.0
    assert row["unit_repaired"]


def test_a_genuine_hourly_wage_is_left_alone():
    row = one(WAGE_RATE_OF_PAY_FROM=60.0, WAGE_UNIT_OF_PAY="Hour")
    assert row["annual_wage"] == 124_800.0
    assert not row["unit_repaired"]


def test_repair_decision_applies_to_both_ends_of_a_band():
    """One decision per row, read from the low end, or the band splits scales."""
    row = one(
        WAGE_RATE_OF_PAY_FROM=150_000.0,
        WAGE_RATE_OF_PAY_TO=170_000.0,
        WAGE_UNIT_OF_PAY="Hour",
    )
    assert row["annual_from"] == 150_000.0
    assert row["annual_to"] == 170_000.0
    assert row["annual_wage"] == 160_000.0


# --------------------------------------------------------------------------
# Wage bands — a third of filings give a range, not a figure
# --------------------------------------------------------------------------


def test_band_reports_the_midpoint():
    assert one(WAGE_RATE_OF_PAY_TO=140_000.0)["annual_wage"] == 120_000.0


def test_band_ends_are_both_kept():
    row = one(WAGE_RATE_OF_PAY_TO=140_000.0)
    assert (row["annual_from"], row["annual_to"]) == (100_000.0, 140_000.0)


def test_outlier_is_judged_on_the_midpoint_not_the_floor():
    """A plausible floor with an implausible midpoint: 84 real rows do this."""
    row = one(WAGE_RATE_OF_PAY_FROM=100_000.0, WAGE_RATE_OF_PAY_TO=9_000_000.0)
    assert row["annual_wage"] == 4_550_000.0
    assert row["is_outlier"]


# --------------------------------------------------------------------------
# Prevailing wage — same defects, separate flags
# --------------------------------------------------------------------------


def test_prevailing_wage_unit_is_repaired_too():
    row = one(PREVAILING_WAGE=173_104.0, PW_UNIT_OF_PAY="Hour")
    assert row["prevailing_wage"] == 173_104.0
    assert row["pw_repaired"]


def test_prevailing_wage_is_flagged_independently_of_the_offer():
    """The exact leak: a sensible offer hiding a nonsense prevailing wage."""
    row = one(WAGE_RATE_OF_PAY_FROM=200_000.0, PREVAILING_WAGE=9_565_244.0)
    assert not row["is_outlier"]
    assert row["pw_outlier"]


def test_prevailing_hourly_wage_annualizes():
    assert one(PREVAILING_WAGE=50.0, PW_UNIT_OF_PAY="Hour")[
        "prevailing_wage"
    ] == 104_000.0


# --------------------------------------------------------------------------
# Failing loudly. Each of these once failed silently instead.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["WAGE_UNIT_OF_PAY", "PW_UNIT_OF_PAY"])
@pytest.mark.parametrize("value", ["Fortnight", None])
def test_unusable_wage_unit_raises(column, value):
    with pytest.raises(ValueError, match=column):
        clean.clean(raw(**{column: value}))


def test_high_end_without_a_low_end_raises():
    """The repair decision is read from the low end; without one it is absent."""
    with pytest.raises(ValueError, match="no _FROM"):
        clean.clean(raw(WAGE_RATE_OF_PAY_FROM=None, WAGE_RATE_OF_PAY_TO=140_000.0))


def test_a_wage_that_is_not_a_number_raises():
    """``"$120,000"`` used to become NaN, and every wage was reported outlier."""
    with pytest.raises(ValueError, match="not numbers"):
        clean.clean(raw(WAGE_RATE_OF_PAY_FROM="$120,000"))


def test_a_blank_wage_is_not_treated_as_unreadable():
    """68% of rows have no WAGE_RATE_OF_PAY_TO. Blank and unreadable differ."""
    assert one(WAGE_RATE_OF_PAY_TO=None)["annual_wage"] == 100_000.0


def test_flags_are_never_pandas_na_for_a_text_wage_column():
    """A text column yields a nullable dtype, and pd.NA survives comparisons.

    An NA flag is silently *dropped* by ``frame[~frame["is_outlier"]]`` rather
    than raising, so it removes rows from a filter without saying so.
    """
    frame = raw()
    frame["WAGE_RATE_OF_PAY_FROM"] = pd.array(["100000"], dtype="string")
    frame["PREVAILING_WAGE"] = pd.array(["95000"], dtype="string")
    cleaned = clean.clean(frame)
    for flag in ("is_outlier", "pw_outlier", "unit_repaired", "pw_repaired"):
        assert cleaned[flag].dtype == bool, flag
        assert not cleaned[flag].isna().any(), flag


# --------------------------------------------------------------------------
# Fiscal year
# --------------------------------------------------------------------------


def test_october_starts_the_next_fiscal_year():
    dates = pd.Series(["2024-09-30", "2024-10-01"])
    assert clean.fiscal_year(dates).tolist() == [2024, 2025]


def test_missing_decision_date_falls_back_to_received_date():
    row = one(DECISION_DATE=None, RECEIVED_DATE="2025-11-01")
    assert row["fiscal_year"] == 2026
    assert pd.isna(row["decision_date"])


def test_fallback_year_is_floored():
    """3 filings were received in FY2023, a year this data does not cover."""
    row = one(DECISION_DATE=None, RECEIVED_DATE="2023-03-21")
    assert row["fiscal_year"] == clean.EARLIEST_FISCAL_YEAR


def test_fiscal_year_does_not_depend_on_what_else_is_in_the_frame():
    """The floor is a constant precisely so batching cannot change an answer.

    Reading it from the frame made a quarter-by-quarter run disagree with a
    single pass on 24 rows, with nothing to indicate it had happened.
    """
    alone = clean.fiscal_year(
        pd.Series([None]), pd.Series(["2025-02-01"])
    ).tolist()
    with_fy2026_neighbours = clean.fiscal_year(
        pd.Series([None, "2026-01-15"]), pd.Series(["2025-02-01", "2025-10-01"])
    ).tolist()
    assert alone[0] == with_fy2026_neighbours[0] == 2025


def test_older_source_data_raises_rather_than_being_truncated():
    with pytest.raises(ValueError, match="EARLIEST_FISCAL_YEAR"):
        clean.fiscal_year(pd.Series(["2022-11-01"]), pd.Series(["2022-01-01"]))


# --------------------------------------------------------------------------
# Row filters
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kept"),
    [
        ("Certified", True),
        ("Certified - Withdrawn", True),
        ("Withdrawn", False),
        ("Denied", False),
    ],
)
def test_only_committed_wages_are_kept(status, kept):
    assert len(clean.clean(raw(CASE_STATUS=status))) == int(kept)


@pytest.mark.parametrize(
    ("soc", "is_tech"),
    [
        ("15-1252", True),
        ("15-2051", True),
        ("11-3021", True),  # IS managers: Management by SOC, tech by job
        ("11-3022", False),
        ("29-1216", False),
    ],
)
def test_tech_filter_covers_major_group_15_plus_is_managers(soc, is_tech):
    assert clean.is_tech(pd.Series([soc]))[0] == is_tech


def test_non_tech_rows_survive_when_the_filter_is_off():
    assert len(clean.clean(raw(SOC_CODE="29-1216"), tech_only=False)) == 1


# --------------------------------------------------------------------------
# The row ledger
# --------------------------------------------------------------------------


def test_stage_counts_accounts_for_every_dropped_row():
    frame = raw(
        CASE_NUMBER=["a", "b", "c"],
        CASE_STATUS=["Certified", "Denied", "Certified"],
        SOC_CODE=["15-1252", "15-1252", "29-1216"],
    )
    counts = clean.stage_counts(frame, rows_read=5)
    assert counts["rows read"] == 5
    assert counts["duplicate cases"] == 2
    assert counts["not certified"] == 1
    assert counts["not tech"] == 1
    assert counts["rows out"] == 1
    assert counts["rows read"] - 2 - 1 - 1 == counts["rows out"]


def test_stage_counts_rejects_a_rows_read_below_the_frame():
    """Dedup only removes rows, so a smaller count is the wrong number.

    The reconciliation cannot catch this: the arithmetic stays consistent and
    the ledger balances around a negative duplicate count.
    """
    with pytest.raises(ValueError, match="below"):
        clean.stage_counts(raw(), rows_read=0)


def test_stage_counts_notices_a_filter_it_does_not_know_about(monkeypatch):
    real = clean.clean
    monkeypatch.setattr(clean, "clean", lambda f, tech_only=True: real(f).iloc[:0])
    with pytest.raises(ValueError, match="stage_counts"):
        clean.stage_counts(raw())


def test_stage_counts_skips_the_pipeline_when_given_its_output(monkeypatch):
    """The whole point of ``cleaned=``: do not clean 1.3M rows twice.

    Asserting the counts match is not enough — they match either way, so a
    parameter that silently did nothing would pass. Make calling ``clean``
    an error instead, which can only be satisfied by actually using the
    frame that was handed over.
    """
    frame = raw(CASE_NUMBER=["a", "b"], CASE_STATUS=["Certified", "Denied"])
    cleaned = clean.clean(frame)

    def fail(*args, **kwargs):
        raise AssertionError("clean() ran even though cleaned= was supplied")

    monkeypatch.setattr(clean, "clean", fail)
    assert clean.stage_counts(frame, cleaned=cleaned)["rows out"] == len(cleaned)


def test_stage_counts_reads_the_same_with_or_without_the_cleaned_frame():
    frame = raw(
        CASE_NUMBER=["a", "b", "c"],
        CASE_STATUS=["Certified", "Denied", "Certified"],
        SOC_CODE=["15-1252", "15-1252", "29-1216"],
    )
    pd.testing.assert_series_equal(
        clean.stage_counts(frame),
        clean.stage_counts(frame, cleaned=clean.clean(frame)),
    )


def test_stage_counts_rejects_a_cleaned_frame_from_other_data():
    """Supplying the frame moves the trust to the caller. Verify it anyway."""
    frame = raw(CASE_NUMBER=["a", "b"])
    with pytest.raises(ValueError, match="passed as cleaned"):
        clean.stage_counts(frame, cleaned=clean.clean(frame).iloc[:0])


# --------------------------------------------------------------------------
# Whole-pipeline properties
# --------------------------------------------------------------------------


def test_clean_does_not_mutate_its_argument():
    frame = raw()
    before = frame.copy()
    clean.clean(frame)
    pd.testing.assert_frame_equal(frame, before)


def test_clean_is_repeatable():
    frame = raw()
    pd.testing.assert_frame_equal(clean.clean(frame), clean.clean(frame))


def test_an_empty_frame_is_not_an_error():
    empty = pd.DataFrame({c: pd.Series(dtype="object") for c in clean.SOURCE_COLUMNS})
    assert len(clean.clean(empty)) == 0


def test_source_columns_lists_everything_clean_reads():
    for column in clean.SOURCE_COLUMNS:
        with pytest.raises(KeyError):
            clean.clean(raw().drop(columns=[column]))
