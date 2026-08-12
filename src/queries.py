"""Every question the dashboard asks the database, one function each.

Two decisions shape all the SQL below.

**Percentiles are computed by hand**, because SQLite has no ``PERCENTILE_CONT``.
Every query ranks its rows with ``ROW_NUMBER()`` and counts them with
``COUNT(*) OVER ()``, then interpolates between the two rows either side of the
position it wants — the same definition pandas, numpy and Excel use, so a
figure on the dashboard is the figure someone gets checking it themselves.

The tempting alternative is to walk up the sorted rows and take the first one
at or past the target. For an odd count that lands on the middle row and is
right; for an even count it always takes the *lower* of the two middle rows,
biasing every even-sized group downward. On real data that moved 34% of city
medians, by up to $12,850, always in the same direction. See :data:`_MEDIAN`.

Phase 2 ports these to Azure SQL's ``PERCENTILE_CONT``, which is this same
definition — so the two backends can be checked against each other exactly
rather than approximately.

**Titles are matched case-insensitively, through the lookup table.** Employers
file the same job under any capitalisation — 3,587 filings say "Data Analyst"
and 777 say "DATA ANALYST" — so an exact match silently loses 17% of them.
Resolving the title against ``titles`` first and filtering ``filings`` on the
resulting ``title_id`` keeps the index in play: matching case-insensitively
against the joined view instead costs 156 ms where this costs 7 ms.

Every value that reaches SQL does so as a ``?`` parameter. The only strings
this module interpolates are its own WHERE clauses, chosen by which arguments
are None.

**Pass a job title.** Filtered, every function here answers in 8-130 ms.
Unfiltered, they rank all 850,321 rows and take 0.7-1.6 seconds, and no index
fixes that: four covering indexes on ``annual_wage`` were measured at +58 MB
for no improvement at all, because SQLite sorts for a window function whether
or not an index could supply the order. :data:`DEFAULT_JOB_TITLE` exists so
the dashboard never opens on the unfiltered path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from src.load import DB_PATH, connect

__all__ = [
    "DEFAULT_JOB_TITLE",
    "salary_percentiles",
    "top_employers",
    "salary_by_city",
    "salary_trend",
    "title_search",
]

# What the dashboard opens on. Any title would do; this is the most filed one,
# at 63,491 filings. Its real job is to keep the unfiltered path — which is
# thirty to a hundred times slower — off the first screen a visitor sees.
DEFAULT_JOB_TITLE = "Software Engineer"

# Resolved through the lookup table so the index on filings(title_id) applies.
_TITLE_MATCHES = (
    "f.title_id IN (SELECT title_id FROM titles WHERE job_title = ? COLLATE NOCASE)"
)


def _percentile(fraction: float) -> str:
    """SQL for one percentile over a ranked CTE exposing ``position`` and ``n``.

    The target sits at position ``1 + fraction * (n - 1)``, which is usually
    between two rows. Each row is weighted by how close it is — ``1`` if it
    lands exactly on the target, ``0`` once it is a full position away — so the
    only rows contributing are the two either side, and their weights sum to 1.
    That is linear interpolation, written as one aggregate rather than a join
    against a computed floor and ceiling.

    The three fractions used here give offsets of .0, .25, .5 or .75, all of
    which are exact in binary, so the weights carry no rounding error.
    """
    target = f"(1 + {fraction} * (n - 1))"
    return f"SUM(annual_wage * max(0.0, 1.0 - abs(position - {target})))"


# The median, by far the most reused of the three.
_MEDIAN = _percentile(0.50)


# Arguments are checked before they reach SQL, because SQLite's response to a
# wrong type is an answer rather than an error: a negative LIMIT means *no
# limit*, a string compared against an INTEGER column matches nothing, and a
# non-empty string is truthy whatever it says. Each of those reads as data.


def _whole_number(name: str, value: Any) -> int:
    """A count or a year. Rejects negatives, non-integers, and ``bool``.

    ``limit=-1`` silently returns 2,428 employers where 20 were asked for, and
    an autocomplete asking for 25 titles gets 5,253, because SQLite reads a
    negative ``LIMIT`` as no limit at all. ``fiscal_year="2025"`` matches no
    rows and reads as "nobody filed that year".

    ``bool`` is excluded deliberately: it subclasses ``int``, so ``limit=True``
    would otherwise quietly mean ``limit=1``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a whole number, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


def _flag(name: str, value: Any) -> bool:
    """A true/false switch, and only that.

    Every non-empty string is truthy, so ``include_outliers="no"`` would turn
    the outlier filter *off* — the opposite of what it says.
    """
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be True or False, got {value!r}")
    return value


def _text(name: str, value: Any) -> str | None:
    """An optional string filter. ``None`` means "do not filter on this"."""
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be text or None, got {value!r}")
    return value


def _escape_like(text: str) -> str:
    r"""Make ``text`` a literal prefix rather than a LIKE pattern.

    ``%`` and ``_`` are wildcards, so someone typing ``C_`` in the title box
    would match ``C#`` and ``C++`` and a plain ``%`` would return everything.
    Parameterising the value stops it being *syntax*; it does not stop it being
    a pattern. The backslash must be escaped first or it would escape itself.
    """
    for character in ("\\", "%", "_"):
        text = text.replace(character, f"\\{character}")
    return text


def _run(sql: str, params: Sequence[Any], db: Path | None) -> pd.DataFrame:
    """Run one query and hand back a DataFrame, closing the connection after."""
    connection = connect(DB_PATH if db is None else db)
    try:
        return pd.read_sql_query(sql, connection, params=list(params))
    finally:
        connection.close()


def _where(
    job_title: str | None = None,
    city: str | None = None,
    state: str | None = None,
    fiscal_year: int | None = None,
    include_outliers: bool = False,
) -> tuple[str, list[Any]]:
    """Build the shared filter. Clauses are literal; values are parameters."""
    clauses = ["f.annual_wage IS NOT NULL"]
    params: list[Any] = []

    if not _flag("include_outliers", include_outliers):
        clauses.append("f.is_outlier = 0")
    if _text("job_title", job_title) is not None:
        clauses.append(_TITLE_MATCHES)
        params.append(job_title)
    if _text("city", city) is not None:
        clauses.append("l.worksite_city = ? COLLATE NOCASE")
        params.append(city)
    if _text("state", state) is not None:
        clauses.append("l.worksite_state = ? COLLATE NOCASE")
        params.append(state)
    if fiscal_year is not None:
        clauses.append("f.fiscal_year = ?")
        params.append(_whole_number("fiscal_year", fiscal_year))

    return " AND ".join(clauses), params


def salary_percentiles(
    job_title: str | None = None,
    city: str | None = None,
    state: str | None = None,
    fiscal_year: int | None = None,
    include_outliers: bool = False,
    db: Path | None = None,
) -> pd.DataFrame:
    """p25, p50, p75 and the filing count for one slice. Always one row.

    An empty slice returns NULL percentiles and ``n_filings`` 0 rather than no
    rows at all, so the dashboard has something to render either way.
    """
    where, params = _where(job_title, city, state, fiscal_year, include_outliers)
    return _run(
        f"""
        WITH ranked AS (
            SELECT f.annual_wage,
                   ROW_NUMBER() OVER (ORDER BY f.annual_wage) AS position,
                   COUNT(*)     OVER ()                       AS n
            FROM filings f
            JOIN locations l ON l.location_id = f.location_id
            WHERE {where}
        )
        SELECT {_percentile(0.25)} AS p25,
               {_MEDIAN}           AS p50,
               {_percentile(0.75)} AS p75,
               COALESCE(MAX(n), 0) AS n_filings
        FROM ranked
        """,
        params,
        db,
    )


def top_employers(
    job_title: str | None = None,
    city: str | None = None,
    limit: int = 20,
    db: Path | None = None,
) -> pd.DataFrame:
    """The employers filing most often for this slice, with their median wage.

    Counted first, then ranked. Only ``limit`` employers survive the count, so
    the window function that follows sorts their filings rather than all
    850,321 — 533 ms instead of 1,335 on the unfiltered case. Narrowing before
    an expensive window function is worth the second CTE.
    """
    where, params = _where(job_title, city)
    return _run(
        f"""
        WITH counted AS (
            SELECT f.employer_id, COUNT(*) AS n_filings
            FROM filings f
            JOIN locations l ON l.location_id = f.location_id
            WHERE {where}
            GROUP BY f.employer_id
            ORDER BY n_filings DESC
            LIMIT ?
        ),
        ranked AS (
            SELECT f.employer_id, f.annual_wage,
                   ROW_NUMBER() OVER (PARTITION BY f.employer_id
                                      ORDER BY f.annual_wage) AS position,
                   COUNT(*)     OVER (PARTITION BY f.employer_id) AS n
            FROM filings f
            JOIN counted c ON c.employer_id = f.employer_id
            JOIN locations l ON l.location_id = f.location_id
            WHERE {where}
        )
        SELECT e.employer_name,
               MAX(n)   AS n_filings,
               {_MEDIAN} AS median_wage
        FROM ranked r
        JOIN employers e ON e.employer_id = r.employer_id
        GROUP BY r.employer_id
        ORDER BY n_filings DESC, e.employer_name
        """,
        [*params, _whole_number("limit", limit), *params],
        db,
    )


def salary_by_city(
    job_title: str | None = None,
    min_filings: int = 10,
    db: Path | None = None,
) -> pd.DataFrame:
    """Median wage per worksite, for cities with enough filings to mean anything.

    ``min_filings`` is the whole point: a city with three filings produces a
    median that swings by tens of thousands on one row, and plotted beside
    Seattle it looks like a finding.
    """
    where, params = _where(job_title)
    return _run(
        f"""
        WITH ranked AS (
            SELECT l.worksite_city, l.worksite_state, f.annual_wage,
                   ROW_NUMBER() OVER (PARTITION BY f.location_id
                                      ORDER BY f.annual_wage) AS position,
                   COUNT(*)     OVER (PARTITION BY f.location_id) AS n
            FROM filings f
            JOIN locations l ON l.location_id = f.location_id
            WHERE {where}
        )
        SELECT worksite_city, worksite_state,
               {_MEDIAN} AS median_wage,
               MAX(n)   AS n_filings
        FROM ranked
        GROUP BY worksite_city, worksite_state
        HAVING n_filings >= ?
        ORDER BY median_wage DESC, worksite_city
        """,
        [*params, _whole_number("min_filings", min_filings)],
        db,
    )


def salary_trend(
    job_title: str | None = None,
    city: str | None = None,
    db: Path | None = None,
) -> pd.DataFrame:
    """Median wage per fiscal year, with the year-over-year change.

    ``yoy_pct_change`` is NULL for the earliest year, which has nothing to
    compare against — that is ``LAG`` behaving correctly, not missing data.

    FY2026 covers two quarters where the others cover four, so its filing
    count is not comparable with the rest. The median is.
    """
    where, params = _where(job_title, city)
    return _run(
        f"""
        WITH ranked AS (
            SELECT f.fiscal_year, f.annual_wage,
                   ROW_NUMBER() OVER (PARTITION BY f.fiscal_year
                                      ORDER BY f.annual_wage) AS position,
                   COUNT(*)     OVER (PARTITION BY f.fiscal_year) AS n
            FROM filings f
            JOIN locations l ON l.location_id = f.location_id
            WHERE {where}
        ),
        per_year AS (
            SELECT fiscal_year,
                   {_MEDIAN} AS median_wage,
                   MAX(n) AS n_filings
            FROM ranked
            GROUP BY fiscal_year
        )
        SELECT fiscal_year, median_wage, n_filings,
               ROUND(100.0 * (median_wage - LAG(median_wage) OVER years)
                     / LAG(median_wage) OVER years, 1) AS yoy_pct_change
        FROM per_year
        WINDOW years AS (ORDER BY fiscal_year)
        ORDER BY fiscal_year
        """,
        params,
        db,
    )


def title_search(
    prefix: str | None = "", limit: int = 25, db: Path | None = None
) -> list[str]:
    """Job titles starting with ``prefix``, most filed first.

    Ordered by popularity rather than alphabetically: the first thing anyone
    types is "data", and they want Data Engineer before DATA  ARCHITECT.

    Spellings that differ only in case are one entry, represented by whichever
    the database happens to return for the group. Any of them finds the same
    filings, because every query here matches titles case-insensitively.

    ``None`` is treated as an empty prefix. A picker with nothing selected
    hands back ``None`` rather than ``""``, and the two mean the same thing
    here — the alternative is an AttributeError surfacing in the browser.

    Outliers are counted here, unlike everywhere else. They are 37 rows in
    850,321 and cannot change an ordering, and excluding them turns a 42 ms
    query into a 164 ms one — which this can least afford, since it runs again
    on every keystroke.
    """
    frame = _run(
        r"""
        SELECT t.job_title, COUNT(*) AS n
        FROM filings f
        JOIN titles t ON t.title_id = f.title_id
        WHERE t.job_title LIKE ? || '%' ESCAPE '\'
        GROUP BY lower(t.job_title)
        ORDER BY n DESC, t.job_title
        LIMIT ?
        """,
        [_escape_like(_text("prefix", prefix) or ""), _whole_number("limit", limit)],
        db,
    )
    return frame["job_title"].tolist()
