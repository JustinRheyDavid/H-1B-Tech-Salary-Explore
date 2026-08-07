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
    "unescape",
    "annualize",
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


def annualize(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Annualize the wage band, repairing wrong unit labels.

    A figure that is implausible once scaled by its unit, but plausible taken
    as-is, is an annual salary filed against the wrong unit. 3,221 rows are
    affected across Hour, Week, Bi-Weekly and Month; left alone they push the
    maximum wage to $1.47 billion.

    The decision is made once per row from the low end and applied to both, so
    a band can never end up with its two sides on different scales.

    Returns ``(annual_from, annual_to, repaired)``.
    """
    unit = frame["WAGE_UNIT_OF_PAY"]

    unknown = set(unit.dropna().unique()) - set(WAGE_MULTIPLIERS)
    n_null = int(unit.isna().sum())
    if unknown or n_null:
        # An unmapped or null unit becomes NaN and the row vanishes from every
        # aggregate without raising. Fail loudly instead.
        raise ValueError(
            f"unmapped WAGE_UNIT_OF_PAY values {unknown or set()}, {n_null} nulls"
        )

    low = pd.to_numeric(frame["WAGE_RATE_OF_PAY_FROM"], errors="coerce")
    high = pd.to_numeric(frame["WAGE_RATE_OF_PAY_TO"], errors="coerce")

    scaled = low * unit.map(WAGE_MULTIPLIERS)
    repaired = (scaled > PLAUSIBLE_HI) & low.between(PLAUSIBLE_LO, PLAUSIBLE_HI)
    multiplier = unit.map(WAGE_MULTIPLIERS).where(~repaired, 1)

    return low * multiplier, high * multiplier, repaired


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


def fiscal_year(dates: pd.Series) -> pd.Series:
    """US federal fiscal year: October 1 starts the next year's count."""
    dates = pd.to_datetime(dates, errors="coerce")
    return (dates.dt.year + (dates.dt.month >= 10).astype("int64")).astype("Int64")


def flag_outliers(wages: pd.Series) -> pd.Series:
    """Mark wages outside the reporting band. Never deletes.

    Must be given the figure actually reported. Flagging the low end instead
    lets through 84 rows whose floor is plausible but whose midpoint is not.
    """
    return ~wages.between(OUTLIER_LO, OUTLIER_HI)


def clean(frame: pd.DataFrame, tech_only: bool = True) -> pd.DataFrame:
    """Run the full pipeline over raw filings.

    Returns one row per filing with normalized columns, ready for the loader.
    Outliers are flagged, not dropped, so the decision stays auditable.
    """
    out = filter_status(frame)

    annual_from, annual_to, repaired = annualize(out)
    has_band = annual_to.notna() & (annual_to > annual_from)
    annual = annual_from.where(~has_band, (annual_from + annual_to) / 2)

    prevailing = pd.to_numeric(out["PREVAILING_WAGE"], errors="coerce") * out[
        "PW_UNIT_OF_PAY"
    ].map(WAGE_MULTIPLIERS)

    soc = normalize_soc(out["SOC_CODE"])

    out = pd.DataFrame(
        {
            "case_number": out["CASE_NUMBER"].astype("string"),
            "case_status": out["CASE_STATUS"].astype("string"),
            "visa_class": out["VISA_CLASS"].astype("string"),
            "decision_date": pd.to_datetime(out["DECISION_DATE"], errors="coerce"),
            "fiscal_year": fiscal_year(out["DECISION_DATE"]),
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
        }
    )

    if tech_only:
        out = out[is_tech(out["soc_code"])]

    return out.reset_index(drop=True)
