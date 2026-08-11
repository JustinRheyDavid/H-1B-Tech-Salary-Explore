"""Every question the dashboard asks the database, one function each.

Two decisions shape all the SQL below.

**Percentiles are computed by hand.** SQLite has no ``PERCENTILE_CONT``, so
each query ranks its rows with ``ROW_NUMBER()``, counts them with ``COUNT(*)
OVER ()``, and takes the first value at or past the target position — the
nearest-rank definition. ``position * 2 >= n`` is the median, ``position * 4
>= n`` the 25th percentile, ``position * 4 >= n * 3`` the 75th. Integer
arithmetic on purpose: no floats, nothing to round the wrong way.

**Titles are matched case-insensitively, through the lookup table.** Employers
file the same job under any capitalisation — 3,587 filings say "Data Analyst"
and 777 say "DATA ANALYST" — so an exact match silently loses 17% of them.
Resolving the title against ``titles`` first and filtering ``filings`` on the
resulting ``title_id`` keeps the index in play: matching case-insensitively
against the joined view instead costs 156 ms where this costs 7 ms.

Every value that reaches SQL does so as a ``?`` parameter. The only strings
this module interpolates are its own WHERE clauses, chosen by which arguments
are None.

**Pass a job title.** Filtered, every function here answers in 8-14 ms.
Unfiltered, they rank all 850,321 rows and take 0.5-1.5 seconds, and no index
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

    if not include_outliers:
        clauses.append("f.is_outlier = 0")
    if job_title is not None:
        clauses.append(_TITLE_MATCHES)
        params.append(job_title)
    if city is not None:
        clauses.append("l.worksite_city = ? COLLATE NOCASE")
        params.append(city)
    if state is not None:
        clauses.append("l.worksite_state = ? COLLATE NOCASE")
        params.append(state)
    if fiscal_year is not None:
        clauses.append("f.fiscal_year = ?")
        params.append(fiscal_year)

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
        SELECT MIN(CASE WHEN position * 4 >= n     THEN annual_wage END) AS p25,
               MIN(CASE WHEN position * 2 >= n     THEN annual_wage END) AS p50,
               MIN(CASE WHEN position * 4 >= n * 3 THEN annual_wage END) AS p75,
               COALESCE(MAX(n), 0)                                       AS n_filings
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
               MAX(n)                                               AS n_filings,
               MIN(CASE WHEN position * 2 >= n THEN annual_wage END) AS median_wage
        FROM ranked r
        JOIN employers e ON e.employer_id = r.employer_id
        GROUP BY r.employer_id
        ORDER BY n_filings DESC, e.employer_name
        """,
        [*params, limit, *params],
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
               MIN(CASE WHEN position * 2 >= n THEN annual_wage END) AS median_wage,
               MAX(n)                                                AS n_filings
        FROM ranked
        GROUP BY worksite_city, worksite_state
        HAVING n_filings >= ?
        ORDER BY median_wage DESC, worksite_city
        """,
        [*params, min_filings],
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
                   MIN(CASE WHEN position * 2 >= n THEN annual_wage END) AS median_wage,
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
    prefix: str = "", limit: int = 25, db: Path | None = None
) -> list[str]:
    """Job titles starting with ``prefix``, most filed first.

    Ordered by popularity rather than alphabetically: the first thing anyone
    types is "data", and they want Data Engineer before DATA  ARCHITECT.

    Spellings that differ only in case are one entry, represented by whichever
    the database happens to return for the group. Any of them finds the same
    filings, because every query here matches titles case-insensitively.

    Outliers are counted here, unlike everywhere else. They are 37 rows in
    850,321 and cannot change an ordering, and excluding them turns a 42 ms
    query into a 164 ms one — which this can least afford, since it runs again
    on every keystroke.
    """
    frame = _run(
        """
        SELECT t.job_title, COUNT(*) AS n
        FROM filings f
        JOIN titles t ON t.title_id = f.title_id
        WHERE t.job_title LIKE ? || '%'
        GROUP BY lower(t.job_title)
        ORDER BY n DESC, t.job_title
        LIMIT ?
        """,
        [prefix, limit],
        db,
    )
    return frame["job_title"].tolist()
