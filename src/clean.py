"""Normalize raw LCA filings into the shape the database expects.

Every rule here answers a defect measured in ``notebooks/01_exploration.ipynb``.
The spec table at the end of that notebook is the authority; this module is
its implementation.

Order matters. Repair units, then annualize, then decide bands, then flag
outliers. Any other order lets bad data through one of the later gates:

* deciding a band before repairing units gives the two ends different scales
* flagging outliers before taking the midpoint misses rows whose floor is
  plausible but whose reported figure is not

Functions take a Series or DataFrame and return a new one. Nothing here does
I/O, mutates its argument, or reads a global.
"""

from __future__ import annotations

import re

import pandas as pd

__all__ = [
    "SOURCE_COLUMNS",
    "to_wage",
    "unescape",
    "repair_units",
    "annualize",
    "annualize_prevailing",
    "normalize_employer",
    "normalize_city",
    "normalize_state",
    "normalize_soc",
    "filter_status",
    "filter_visa",
    "is_tech",
    "fiscal_year",
    "flag_outliers",
    "clean",
    "stage_counts",
]

# Every raw column :func:`clean` reads. Callers pass this to ``ingest.load_all``
# so a missing column fails at read time rather than deep inside the pipeline;
# reading all 98 columns instead costs about 7 GB across the nine files.
#
# notebooks/01_exploration.ipynb keeps its own shorter list on purpose. It is a
# record of what Step 3 measured, and editing it to track this module would
# make its saved output describe a run that never happened.
SOURCE_COLUMNS = [
    "CASE_NUMBER",
    "CASE_STATUS",
    "VISA_CLASS",
    "DECISION_DATE",
    "RECEIVED_DATE",
    "EMPLOYER_NAME",
    "JOB_TITLE",
    "SOC_CODE",
    "SOC_TITLE",
    "WORKSITE_CITY",
    "WORKSITE_STATE",
    "WAGE_RATE_OF_PAY_FROM",
    "WAGE_RATE_OF_PAY_TO",
    "WAGE_UNIT_OF_PAY",
    "PREVAILING_WAGE",
    "PW_UNIT_OF_PAY",
    "FULL_TIME_POSITION",
]

# Filed wage units and what a year's worth of each is.
WAGE_MULTIPLIERS = {"Year": 1, "Hour": 2080, "Month": 12, "Week": 52, "Bi-Weekly": 26}

# Two thresholds that share values today but answer different questions.
# Keeping them separate means retuning the reporting band cannot silently
# change which rows get repaired.
PLAUSIBLE_LO, PLAUSIBLE_HI = 10_000, 2_000_000  # could a person earn this in a year?
OUTLIER_LO, OUTLIER_HI = 10_000, 2_000_000  # reporting band behind is_outlier

# Only these represent a wage an employer actually committed to.
KEEP_STATUSES = ("Certified", "Certified - Withdrawn")

# The earliest fiscal year the nine source files decide anything in. A filing
# published in them was decided no earlier than this, whatever its received
# date says.
#
# A constant, not ``min()`` over the frame. Reading it from the data makes a
# row's fiscal year depend on which other rows happened to be passed with it,
# so cleaning quarter by quarter and cleaning in one pass disagree on 24 rows
# with nothing to indicate it.
#
# Update this when the source files change which years they cover.
# :func:`fiscal_year` raises if the data decides anything earlier, so adding
# older files cannot leave it stale. Retiring the oldest files cannot be caught
# the same way — a frame whose earliest year is FY2027 is indistinguishable
# from one quarter of a dataset that still starts at FY2024, and refusing the
# latter would break chunked loading to guard against a hypothetical.
EARLIEST_FISCAL_YEAR = 2024

# 15-xxxx is Computer and Mathematical Occupations; 11-3021 is Computer and
# Information Systems Managers, which sits under Management but is a tech role.
TECH_SOC_MAJOR = "15"
TECH_SOC_EXTRA = ("11-3021",)

# Excel wraps any value containing a double quote in a formula escape:
#   ="Financial Planning and Analysis ("FP&A") Manager"
# Anchored at both ends on purpose. An unanchored ^="|"$ alternation also
# strips the trailing quote from a legitimate title like:  Analyst "Senior"
_ESCAPED = re.compile(r'^="(.*)"$', re.DOTALL)

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")
_LEGAL_SUFFIX = re.compile(
    r"\s+(INC|LLC|LTD|CORP|CORPORATION|CO|LP|LLP|PC|PLLC)$"
)


def unescape(values: pd.Series) -> pd.Series:
    """Remove Excel's ``="..."`` wrapper, leaving inner quotes intact.

    Affects JOB_TITLE and SOC_CODE, about 130,000 rows each. Skipping it on
    SOC_CODE silently drops 9.5% of tech filings from any prefix match.
    """
    return values.astype("string").str.replace(_ESCAPED, r"\1", regex=True)


def to_wage(values: pd.Series, column: str) -> pd.Series:
    """Parse a wage column to plain ``float64``, refusing to lose a value.

    Two guarantees, both of which exist because their absence fails quietly.

    The cast, first. ``pd.to_numeric`` hands back a *nullable* dtype when given
    a ``string`` column — ``Int64`` for whole numbers, ``Float64`` for decimals
    — and ``pd.NA``, unlike ``NaN``, survives a comparison instead of
    collapsing to False. That turns every downstream boolean into ``NA``, and
    a row with an ``NA`` flag is dropped from ``frame[~frame["is_outlier"]]``
    silently rather than raising. Casting to ``float64`` settles the question
    here instead of leaving it to whatever dtype the Parquet cache happens to
    hold.

    The check, second. ``errors="coerce"`` turns anything it cannot read into
    ``NaN``, so a column arriving as ``"$120,000"`` becomes a column of nulls
    and the pipeline reports every wage as an outlier without complaint. A
    genuinely blank cell is fine and common — ``WAGE_RATE_OF_PAY_TO`` is empty
    on 68% of rows. A cell that holds something unreadable is not, and gets
    the same loud failure as an unmapped wage unit.
    """
    parsed = pd.to_numeric(values, errors="coerce").astype("float64")
    lost = int((values.notna() & parsed.isna()).sum())
    if lost:
        sample = values[values.notna() & parsed.isna()].unique()[:5].tolist()
        raise ValueError(
            f"{column}: {lost} non-blank values are not numbers, e.g. {sample}"
        )
    return parsed


def repair_units(
    values: pd.Series, unit: pd.Series, column: str
) -> tuple[pd.Series, pd.Series]:
    """Multiplier to annualize ``values``, with wrong unit labels repaired.

    A figure that is implausible once scaled by its unit, but plausible taken
    as-is, is an annual figure filed against the wrong unit. Such a row gets a
    multiplier of 1 instead of its label's.

    Returns ``(multiplier, repaired)`` rather than the annualized values,
    because a wage band has to take one decision from its low end and apply it
    to both ends. Handing back the multiplier is what makes that possible.
    """
    unknown = set(unit.dropna().unique()) - set(WAGE_MULTIPLIERS)
    n_null = int(unit.isna().sum())
    if unknown or n_null:
        # An unmapped or null unit becomes NaN and the row vanishes from every
        # aggregate without raising. Fail loudly instead.
        raise ValueError(f"unmapped {column} values {unknown or set()}, {n_null} nulls")

    # A figure above PLAUSIBLE_HI / multiplier is read as mislabelled, so a
    # genuine salary over $2M filed monthly would be divided by 12. The largest
    # repair in the nine files lands at $705,000, nowhere near the ceiling.
    scale = unit.map(WAGE_MULTIPLIERS)
    repaired = (values * scale > PLAUSIBLE_HI) & values.between(
        PLAUSIBLE_LO, PLAUSIBLE_HI
    )
    return scale.where(~repaired, 1), repaired


def annualize(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Annualize the offered wage band, repairing wrong unit labels.

    1,851 of the 1,315,799 certified filings are affected across Hour, Week,
    Bi-Weekly and Month — 3,221 before the status filter, which is the figure
    the notebook quotes. Left alone they push the maximum wage to $1.47 billion.

    The decision is made once per row from the low end and applied to both, so
    a band can never end up with its two sides on different scales.

    Returns ``(annual_from, annual_to, repaired)``.
    """
    low = to_wage(frame["WAGE_RATE_OF_PAY_FROM"], "WAGE_RATE_OF_PAY_FROM")
    high = to_wage(frame["WAGE_RATE_OF_PAY_TO"], "WAGE_RATE_OF_PAY_TO")

    # Reading the decision from the low end leaves the high end unrepaired if
    # only the high end is present. No such row exists in the nine files, but
    # it is one filing away from being true, and it would fail silently.
    orphan = int((low.isna() & high.notna()).sum())
    if orphan:
        raise ValueError(
            f"{orphan} rows have WAGE_RATE_OF_PAY_TO but no _FROM; the unit "
            "repair decision cannot be read from the low end"
        )

    multiplier, repaired = repair_units(
        low, frame["WAGE_UNIT_OF_PAY"], "WAGE_UNIT_OF_PAY"
    )
    return low * multiplier, high * multiplier, repaired


def annualize_prevailing(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Annualize the prevailing wage, under the same rules as the offered wage.

    This column is filed on the same form with the same defects: of the
    1,315,799 certified filings, 9 carry an annual figure labelled Week (5) or
    Hour (4), which unrepaired put the maximum prevailing wage at $360,056,320.

    Repair does not save every row. 5 remain above the plausible ceiling with
    no unit that makes them sensible — the source figure is simply wrong. They
    are flagged by the caller, not dropped.

    Returns ``(prevailing, repaired)``.
    """
    value = to_wage(frame["PREVAILING_WAGE"], "PREVAILING_WAGE")
    multiplier, repaired = repair_units(
        value, frame["PW_UNIT_OF_PAY"], "PW_UNIT_OF_PAY"
    )
    return value * multiplier, repaired


def normalize_employer(names: pd.Series) -> pd.Series:
    """Collapse case, punctuation, and trailing legal suffixes.

    Deliberately conservative. Fuzzy matching would merge companies that only
    share a substring: ``SparkCognizant Inc`` and ``TMG HEALTH - A COGNIZANT
    COMPANY`` are unrelated to Cognizant Technology Solutions.
    """
    return (
        unescape(names)
        .str.upper()
        .str.replace(_PUNCTUATION, "", regex=True)
        .str.replace(_WHITESPACE, " ", regex=True)
        .str.strip()
        .str.replace(_LEGAL_SUFFIX, "", regex=True)
    )


def normalize_city(cities: pd.Series) -> pd.Series:
    """Title-case city names. Case variation alone invents 6,869 cities."""
    return unescape(cities).str.strip().str.title()


def normalize_state(states: pd.Series) -> pd.Series:
    """Upper-case two-letter state and territory codes."""
    return unescape(states).str.strip().str.upper()


def normalize_soc(codes: pd.Series) -> pd.Series:
    """Unescape and truncate to the seven-character SOC code.

    Filed codes carry a detail suffix (``15-1252.00``) that splits one
    occupation across many values: 1,145 full codes collapse to 677.
    """
    return unescape(codes).str.strip().str.slice(0, 7)


def is_tech(soc_codes: pd.Series) -> pd.Series:
    """True for Computer and Mathematical occupations, plus IS managers.

    Expects codes that have already been through :func:`normalize_soc`.
    """
    major = soc_codes.str.slice(0, 2).eq(TECH_SOC_MAJOR)
    extra = soc_codes.str.startswith(TECH_SOC_EXTRA, na=False)
    return (major | extra).fillna(False)


def filter_status(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep only filings representing a wage an employer committed to.

    Withdrawn and Denied cases are 7.5% of rows and never became an offer.
    """
    return frame[frame["CASE_STATUS"].isin(KEEP_STATUSES)]


def filter_visa(frame: pd.DataFrame, visa_class: str = "H-1B") -> pd.DataFrame:
    """Keep one visa class. E-3 and H-1B1 share the same form, about 3%."""
    return frame[frame["VISA_CLASS"] == visa_class]


def fiscal_year(dates: pd.Series, fallback: pd.Series | None = None) -> pd.Series:
    """US federal fiscal year: October 1 starts the next year's count.

    2,216 filings have no ``DECISION_DATE``. Every one of them carries a
    ``RECEIVED_DATE``, so passing it as ``fallback`` keeps the row rather than
    losing it to a NULL in a column the schema declares NOT NULL. The year is
    then when the filing was received rather than decided — a different
    question, for 0.16% of rows, and the README says so.

    A fallback year is floored at :data:`EARLIEST_FISCAL_YEAR`. A case can be
    received long before it is decided, and 3 filings were received in FY2023 —
    a year these files do not otherwise cover. Without the floor those 3 rows
    open a fiscal-year bucket of their own, and a trend chart plots a median
    drawn from 3 filings beside one drawn from 500,000.

    The floor is one-sided because the error is. A case is received before it
    is decided, never after, so the received year is a lower bound on the true
    decision year — it can be too early but never too late.
    """
    dates = pd.to_datetime(dates, errors="coerce")
    years = dates.dt.year + (dates.dt.month >= 10).astype("int64")

    if years.notna().any() and years.min() < EARLIEST_FISCAL_YEAR:
        # Older source files were added and the constant was not updated, so
        # the floor is now silently truncating real years.
        raise ValueError(
            f"decision dates reach FY{int(years.min())}, before "
            f"EARLIEST_FISCAL_YEAR={EARLIEST_FISCAL_YEAR}; update the constant"
        )

    if fallback is not None:
        spare = pd.to_datetime(fallback, errors="coerce")
        spare_years = spare.dt.year + (spare.dt.month >= 10).astype("int64")
        years = years.fillna(spare_years.clip(lower=EARLIEST_FISCAL_YEAR))

    return years.astype("Int64")


def flag_outliers(wages: pd.Series) -> pd.Series:
    """Mark wages outside the reporting band, and wages there are none of.

    Must be given the figure actually reported. Flagging the low end instead
    lets through 84 rows whose floor is plausible but whose midpoint is not.

    A missing wage is flagged too, since ``NaN`` fails the band test. So the
    count means "not a usable figure", which is wider than "too big or too
    small" — worth knowing before quoting it. No row in the nine files has a
    missing wage, so today the two readings give the same number.
    """
    return ~wages.between(OUTLIER_LO, OUTLIER_HI)


def clean(frame: pd.DataFrame, tech_only: bool = True) -> pd.DataFrame:
    """Run the full pipeline over raw filings.

    Returns one row per filing with normalized columns, ready for the loader.
    Outliers are flagged, not dropped, so the decision stays auditable.

    The offered wage and the prevailing wage are repaired and flagged
    independently: a filing can carry a sensible offer against a nonsense
    prevailing wage, and one flag covering both would hide that.
    """
    out = filter_status(frame)

    annual_from, annual_to, repaired = annualize(out)
    has_band = annual_to.notna() & (annual_to > annual_from)
    annual = annual_from.where(~has_band, (annual_from + annual_to) / 2)

    prevailing, pw_repaired = annualize_prevailing(out)

    soc = normalize_soc(out["SOC_CODE"])

    out = pd.DataFrame(
        {
            "case_number": out["CASE_NUMBER"].astype("string"),
            "case_status": out["CASE_STATUS"].astype("string"),
            "visa_class": out["VISA_CLASS"].astype("string"),
            "decision_date": pd.to_datetime(out["DECISION_DATE"], errors="coerce"),
            "fiscal_year": fiscal_year(out["DECISION_DATE"], out["RECEIVED_DATE"]),
            "employer_name": normalize_employer(out["EMPLOYER_NAME"]),
            "employer_raw": out["EMPLOYER_NAME"].astype("string"),
            "job_title": unescape(out["JOB_TITLE"]),
            "soc_code": soc,
            "soc_title": unescape(out["SOC_TITLE"]),
            "worksite_city": normalize_city(out["WORKSITE_CITY"]),
            "worksite_state": normalize_state(out["WORKSITE_STATE"]),
            "annual_from": annual_from,
            "annual_to": annual_to,
            "annual_wage": annual,
            "prevailing_wage": prevailing,
            "full_time": out["FULL_TIME_POSITION"].eq("Y"),
            "unit_repaired": repaired,
            "is_outlier": flag_outliers(annual),
            "pw_repaired": pw_repaired,
            "pw_outlier": flag_outliers(prevailing),
        }
    )

    if tech_only:
        out = out[is_tech(out["soc_code"])]

    return out.reset_index(drop=True)


def stage_counts(
    frame: pd.DataFrame, tech_only: bool = True, rows_read: int | None = None
) -> pd.Series:
    """Attribute every discarded row to the rule that discarded it.

    :func:`clean` drops more than a third of what it is given, and the read
    before it drops far more again. A single before-and-after number invites
    the reader to assume a bug, so this itemizes the difference.

    ``frame`` is post-deduplication, which is where :func:`clean` starts.
    Pass ``rows_read`` — the row count before ``ingest.load_all`` deduplicated,
    1,367,976 across the nine files — to open the ledger there instead. The
    3,610,511 blank padding rows are dropped earlier still, on read, and are
    counted in ``notebooks/01_exploration.ipynb`` rather than here.

    ``rows out`` comes from :func:`clean` itself, not from re-applying its
    filters, and the stages are reconciled against it. A ledger that can drift
    from the thing it describes is worse than no ledger, so a filter added to
    :func:`clean` and not here fails loudly instead of quietly balancing.

    Returns counts rather than printing them; nothing else here does I/O.
    """
    certified = filter_status(frame)
    n_uncertified = len(frame) - len(certified)
    n_tech = (
        int(is_tech(normalize_soc(certified["SOC_CODE"])).sum())
        if tech_only
        else len(certified)
    )
    n_untech = len(certified) - n_tech
    rows_out = len(clean(frame, tech_only=tech_only))

    stages: dict[str, int] = {}
    start = len(frame)
    dropped = n_uncertified + n_untech
    if rows_read is not None:
        if rows_read < len(frame):
            # Deduplication only ever removes rows, so a smaller count is the
            # wrong number — the pre-dedupe total for a different set of files,
            # or a post-dedupe one passed by mistake. The reconciliation below
            # cannot catch it: the arithmetic stays consistent and the ledger
            # balances around a negative.
            raise ValueError(
                f"rows_read={rows_read:,} is below the {len(frame):,} rows given; "
                "it should be the count before ingest.load_all deduplicated"
            )
        stages["rows read"] = rows_read
        stages["duplicate cases"] = rows_read - len(frame)
        start = rows_read
        dropped += rows_read - len(frame)

    stages["unique filings"] = len(frame)
    stages["not certified"] = n_uncertified
    stages["not tech"] = n_untech
    stages["rows out"] = rows_out

    if start - dropped != rows_out:
        raise ValueError(
            f"stages account for {start - dropped:,} rows but clean() returns "
            f"{rows_out:,}; a filter was added to clean() and not to stage_counts"
        )
    return pd.Series(stages, dtype="int64")
