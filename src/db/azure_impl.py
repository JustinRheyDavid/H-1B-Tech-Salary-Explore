"""The Azure SQL backend. Same seven answers, different dialect.

Plan Step 8. Every query here is a port of the corresponding function in
:mod:`src.queries`, and the two are expected to agree **to the dollar** — not
approximately — which is what ``tests/test_db.py`` asserts.

That exact agreement is possible because of a decision Phase 1 made: its
percentiles interpolate linearly between the two rows either side of
``1 + fraction * (n - 1)``, which is ``PERCENTILE_CONT``'s definition exactly,
and the one pandas, numpy and Excel use. Phase 1 chose it over the simpler
"first row at or past the target", which biases every even-sized group downward
and moved 34% of city medians by up to $12,850.

What the plan could not check from a laptop, and this step exists to settle, is
whether *Azure's implementation* matches that definition. It does — see
``test_percentile_cont_matches_phase_1s_interpolation``.

Four things bite when porting, and all four are parse errors rather than wrong
answers, so they surface immediately:

* **``GROUP BY`` strictness.** SQLite permits a bare column in a grouped query
  and an alias in ``GROUP BY``/``HAVING``. T-SQL permits neither. Four of the
  seven queries had to be restructured, not merely retyped.
* **``PERCENTILE_CONT`` is window-only.** It has no plain-aggregate form and
  returns one value per *row*, not per group, so every per-group median needs
  ``SELECT DISTINCT`` over the partitioned form.
* **``LIMIT n`` becomes ``TOP (n)``**, and the parameter moves to the front of
  the statement — which reorders the parameter list, not just the SQL.
* **``BIN2`` makes ``LIKE`` case-sensitive.** See :func:`_escape_like` and
  :meth:`AzureBackend.title_search`.
"""

from __future__ import annotations

import os
import struct
import warnings
from typing import Any

import pandas as pd

from src import queries

__all__ = ["AzureBackend", "connect", "server", "database"]

# SQL_COPT_SS_ACCESS_TOKEN. Not exported by pyodbc, so the numeric constant is
# the documented way to pass an Entra token through the ODBC driver.
_TOKEN_ATTR = 1256

_SERVER_ENV = "AZURE_SQL_SERVER"
_DATABASE_ENV = "AZURE_SQL_DATABASE"
_DEFAULT_SERVER = "sql-h1b-hutymqa65yoty.database.windows.net"
_DEFAULT_DATABASE = "sqldb-h1b"

# Case-insensitive comparison, stated per query.
#
# Phase 1 writes COLLATE NOCASE. Here the columns are BIN2 — byte-for-byte — so
# that the 9,286 titles differing only by case or trailing whitespace can exist
# at all. The cost is that case-insensitivity stops being a default and becomes
# something every comparison has to ask for.
_CI = "Latin1_General_CI_AS"

# Resolved through the lookup table so the index on filings(title_id) applies,
# exactly as Phase 1 does. Matching case-insensitively against the joined view
# instead cost 156 ms where this costs 7 ms on SQLite.
_TITLE_MATCHES = (
    f"f.title_id IN (SELECT title_id FROM titles "
    f"WHERE job_title COLLATE {_CI} = ?)"
)

# One median, as a window function over a partition.
#
# PERCENTILE_CONT has no aggregate form: Microsoft's reference is explicit that
# the ORDER BY and rows/range parts of OVER cannot be specified, because it is
# strictly a window function. It therefore returns one value per ROW, and every
# caller below collapses that with SELECT DISTINCT.
def _percentile(fraction: float, partition: str = "") -> str:
    over = f"PARTITION BY {partition}" if partition else ""
    return (
        f"PERCENTILE_CONT({fraction}) WITHIN GROUP (ORDER BY f.annual_wage) "
        f"OVER ({over})"
    )


def server() -> str:
    """Logical server FQDN, from ``AZURE_SQL_SERVER`` or the deployed default."""
    return os.environ.get(_SERVER_ENV) or _DEFAULT_SERVER


def database() -> str:
    """Database name, from ``AZURE_SQL_DATABASE`` or the deployed default."""
    return os.environ.get(_DATABASE_ENV) or _DEFAULT_DATABASE


def connect(server_name: str | None = None, database_name: str | None = None):
    """Open a passwordless connection to Azure SQL.

    Identical code locally and in Azure. ``DefaultAzureCredential`` returns your
    ``az login`` token on a laptop and the managed identity's token inside the
    container app — there is no branch, no connection string with a password in
    it, and no secret to rotate. The server has
    ``azureADOnlyAuthentication: true``, so password auth is not merely
    discouraged: there is no password to use.

    The token is passed as an ODBC connection attribute rather than in the
    connection string, which is why it has to be UTF-16-LE encoded and prefixed
    with its own length — that is the layout the driver expects, not a quirk of
    this project.

    **A first connection after an idle hour will fail**, not hang: the database
    is serverless with ``autoPauseDelay: 60`` and answers
    ``Database ... is not currently available`` while it resumes. That is the
    mechanism keeping idle cost at zero, not a fault. Callers that must survive
    it should retry rather than treat it as fatal.
    """
    import pyodbc
    from azure.identity import DefaultAzureCredential

    token = (
        DefaultAzureCredential()
        .get_token("https://database.windows.net/.default")
        .token.encode("utf-16-le")
    )
    packed = struct.pack("<I", len(token)) + token
    connection_string = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={server_name or server()};"
        f"Database={database_name or database()};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    return pyodbc.connect(connection_string, attrs_before={_TOKEN_ATTR: packed})


def _escape_like(text: str) -> str:
    r"""Make ``text`` a literal prefix rather than a LIKE pattern.

    **This is the one piece of Phase 1 logic that genuinely forks per backend.**
    ``queries._escape_like`` escapes ``\``, ``%`` and ``_``, which is every
    wildcard SQLite's ``LIKE`` has. T-SQL adds a character class: ``[abc]``
    matches one of three characters and ``[a-z]`` a range.

    **945 titles contain ``[``** — ``Network Protocol Engineer [Senior]``,
    ``Sr Business Systems Analyst [00058036]``. Without escaping it, a prefix
    typed into the search box that reaches one of them is read as a pattern
    rather than as text, and the two backends return different rows for the same
    keystrokes. Escaping ``]`` is unnecessary — it is literal unless a class is
    open — and the SQLite side must *not* escape either, or it would escape a
    character that is already literal there.
    """
    for character in ("\\", "%", "_", "["):
        text = text.replace(character, f"\\{character}")
    return text


class AzureBackend:
    """Phase 1's seven questions, answered by Azure SQL."""

    DEFAULT_JOB_TITLE = queries.DEFAULT_JOB_TITLE

    def __init__(self, server_name: str | None = None, database_name: str | None = None):
        self.server = server_name or server()
        self.database = database_name or database()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AzureBackend(server={self.server!r}, database={self.database!r})"

    @property
    def TROUBLE(self) -> tuple[type[BaseException], ...]:  # noqa: N802 - interface name
        """What ``app.py`` must catch.

        Imported lazily so that merely importing this module does not require
        pyodbc — the SQLite backend must keep working on a machine with no ODBC
        driver installed, which is most machines.

        ``ClientAuthenticationError`` is in here because a failure to *get* a
        token looks nothing like a database error but reaches the user the same
        way: as a blank dashboard.
        """
        import pyodbc
        from azure.core.exceptions import ClientAuthenticationError

        return (pyodbc.Error, ClientAuthenticationError)

    def _run(self, sql: str, params: list[Any]) -> pd.DataFrame:
        """Run one query and hand back a DataFrame, closing the connection after.

        pandas warns that it only *tests* SQLAlchemy connectables and advises
        using one. Suppressed rather than obeyed: SQLAlchemy would be a third
        dependency in the ETL container to wrap raw SQL that is already written,
        parameterised and tested against the real database. The warning is about
        pandas' test coverage, not about correctness, and left unfiltered it
        fires on every single query — including once per keystroke in
        ``title_search``.
        """
        connection = connect(self.server, self.database)
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="pandas only supports SQLAlchemy connectable",
                    category=UserWarning,
                )
                return pd.read_sql_query(sql, connection, params=params)
        finally:
            connection.close()

    def _where(
        self,
        job_title: str | None = None,
        city: str | None = None,
        state: str | None = None,
        fiscal_year: int | None = None,
        include_outliers: bool = False,
    ) -> tuple[str, list[Any]]:
        """The shared filter. Clauses are literal; values are parameters.

        Validation is Phase 1's, imported rather than reimplemented: the checks
        in ``queries`` exist because a wrong type produces an *answer* rather
        than an error, and that reasoning is dialect-independent. Only
        :func:`_escape_like` had to fork.
        """
        clauses = ["f.annual_wage IS NOT NULL"]
        params: list[Any] = []

        if not queries._flag("include_outliers", include_outliers):
            clauses.append("f.is_outlier = 0")
        if queries._text("job_title", job_title) is not None:
            clauses.append(_TITLE_MATCHES)
            params.append(job_title)
        if queries._text("city", city) is not None:
            clauses.append(f"l.worksite_city COLLATE {_CI} = ?")
            params.append(city)
        if queries._text("state", state) is not None:
            clauses.append(f"l.worksite_state COLLATE {_CI} = ?")
            params.append(state)
        if fiscal_year is not None:
            clauses.append("f.fiscal_year = ?")
            params.append(queries._whole_number("fiscal_year", fiscal_year))

        return " AND ".join(clauses), params

    def salary_percentiles(
        self,
        job_title: str | None = None,
        city: str | None = None,
        state: str | None = None,
        fiscal_year: int | None = None,
        include_outliers: bool = False,
    ) -> pd.DataFrame:
        """p25, p50, p75 and the filing count for one slice. Always one row.

        The ``UNION ALL`` is the "always one row" guarantee, and it is not
        decoration. Phase 1 aggregates, and an aggregate over an empty set still
        returns one row of NULLs. ``SELECT DISTINCT`` over a window function
        returns *no rows* for an empty slice, so without the second branch an
        over-filtered dashboard would get an empty DataFrame and raise
        ``IndexError`` on ``.iloc[0]`` instead of rendering "no filings".
        """
        where, params = self._where(job_title, city, state, fiscal_year, include_outliers)
        return self._run(
            f"""
            WITH computed AS (
                SELECT DISTINCT
                       {_percentile(0.25)} AS p25,
                       {_percentile(0.50)} AS p50,
                       {_percentile(0.75)} AS p75,
                       COUNT(*) OVER ()    AS n_filings
                FROM filings f
                JOIN locations l ON l.location_id = f.location_id
                WHERE {where}
            )
            SELECT p25, p50, p75, n_filings FROM computed
            UNION ALL
            SELECT CAST(NULL AS float), CAST(NULL AS float), CAST(NULL AS float), 0
            WHERE NOT EXISTS (SELECT 1 FROM computed)
            """,
            params,
        )

    def fiscal_years(self) -> list[int]:
        """Every fiscal year in the data, newest first, for the year picker."""
        frame = self._run(
            "SELECT DISTINCT fiscal_year FROM filings ORDER BY fiscal_year DESC", []
        )
        return [int(year) for year in frame["fiscal_year"]]

    def wage_distribution(
        self,
        job_title: str | None = None,
        city: str | None = None,
        state: str | None = None,
        fiscal_year: int | None = None,
        include_outliers: bool = False,
        bin_width: int = 10_000,
    ) -> pd.DataFrame:
        """Filing counts per wage band, for the distribution chart.

        ``bin_floor`` is a select alias and T-SQL will not group by one. The
        obvious fix — repeating the ``CAST`` expression in ``GROUP BY`` — does
        **not** work when the expression contains parameters:

            Column 'filings.annual_wage' is invalid in the select list because
            it is not contained in either an aggregate function or the GROUP BY
            clause. (8120)

        The two expressions are textually identical, but each ``?`` is a distinct
        parameter marker, so the optimizer does not recognise them as the same
        expression. Computing the bin once in a CTE sidesteps the question
        entirely, and binds the width twice instead of four times.

        Both dialects do integer division here (``annual_wage`` and the width are
        both integers), so the bins land identically without a ``FLOOR``.
        """
        where, params = self._where(job_title, city, state, fiscal_year, include_outliers)
        width = queries._whole_number("bin_width", bin_width)
        if width == 0:
            raise ValueError("bin_width must not be zero")
        return self._run(
            f"""
            WITH binned AS (
                SELECT CAST(f.annual_wage / ? AS INT) * ? AS bin_floor
                FROM filings f
                JOIN locations l ON l.location_id = f.location_id
                WHERE {where}
            )
            SELECT bin_floor, COUNT(*) AS n_filings
            FROM binned
            GROUP BY bin_floor
            ORDER BY bin_floor
            """,
            [width, width, *params],
        )

    def top_employers(
        self,
        job_title: str | None = None,
        city: str | None = None,
        limit: int = 20,
    ) -> pd.DataFrame:
        """The employers filing most often for this slice, with their median wage.

        Phase 1 selects ``e.employer_name`` while grouping by ``r.employer_id``,
        which is error 8120 in T-SQL. Rather than widen the ``GROUP BY``, the
        count comes from ``counted`` — which already computed it — and the median
        joins in from a separate CTE. That removes the grouped select entirely.

        **Ties at the cutoff are arbitrary in both backends.** ``TOP (n)`` ordered
        only by ``n_filings DESC`` breaks a tie however the engine likes, exactly
        as Phase 1's ``LIMIT`` does, so the two can legitimately pick different
        employers when the nth and n+1th are tied. Not worth forcing: the
        dashboard shows a leaderboard, not an audit.
        """
        where, params = self._where(job_title, city)
        return self._run(
            f"""
            WITH counted AS (
                SELECT TOP (?) f.employer_id, COUNT(*) AS n_filings
                FROM filings f
                JOIN locations l ON l.location_id = f.location_id
                WHERE {where}
                GROUP BY f.employer_id
                ORDER BY n_filings DESC
            ),
            medians AS (
                SELECT DISTINCT f.employer_id,
                       {_percentile(0.50, "f.employer_id")} AS median_wage
                FROM filings f
                JOIN counted c   ON c.employer_id = f.employer_id
                JOIN locations l ON l.location_id = f.location_id
                WHERE {where}
            )
            SELECT e.employer_name, c.n_filings, m.median_wage
            FROM counted c
            JOIN medians m   ON m.employer_id = c.employer_id
            JOIN employers e ON e.employer_id = c.employer_id
            ORDER BY c.n_filings DESC, e.employer_name
            """,
            [queries._whole_number("limit", limit), *params, *params],
        )

    def salary_by_city(
        self,
        job_title: str | None = None,
        min_filings: int = 10,
    ) -> pd.DataFrame:
        """Median wage per worksite, for cities with enough filings to mean anything.

        Phase 1 writes ``HAVING n_filings >= ?`` against a select alias, which
        T-SQL rejects; the count is repeated as ``COUNT(*)`` in the ``HAVING``.

        Grouping is by ``location_id`` rather than by ``(city, state)``. They are
        one-for-one — ``locations`` has a UNIQUE constraint on the pair — and the
        id is what ``filings`` is indexed on.
        """
        where, params = self._where(job_title)
        return self._run(
            f"""
            WITH per_location AS (
                SELECT f.location_id, COUNT(*) AS n_filings
                FROM filings f
                JOIN locations l ON l.location_id = f.location_id
                WHERE {where}
                GROUP BY f.location_id
                HAVING COUNT(*) >= ?
            ),
            medians AS (
                SELECT DISTINCT f.location_id,
                       {_percentile(0.50, "f.location_id")} AS median_wage
                FROM filings f
                JOIN per_location p ON p.location_id = f.location_id
                JOIN locations l    ON l.location_id = f.location_id
                WHERE {where}
            )
            SELECT l.worksite_city, l.worksite_state, m.median_wage, p.n_filings
            FROM per_location p
            JOIN medians m   ON m.location_id = p.location_id
            JOIN locations l ON l.location_id = p.location_id
            ORDER BY m.median_wage DESC, l.worksite_city
            """,
            [*params, queries._whole_number("min_filings", min_filings), *params],
        )

    def salary_trend(
        self,
        job_title: str | None = None,
        city: str | None = None,
    ) -> pd.DataFrame:
        """Median wage per fiscal year, with the year-over-year change.

        The named ``WINDOW`` clause is kept from Phase 1, and it is the reason
        ``sql/schema_azure.sql`` asserts compatibility level 160+ — below that
        this is a syntax error that reads like a bug in the query rather than a
        database setting.

        ``yoy_pct_change`` is NULL for the earliest year: ``LAG`` behaving
        correctly, not missing data.
        """
        where, params = self._where(job_title, city)
        return self._run(
            f"""
            WITH per_year AS (
                SELECT DISTINCT f.fiscal_year,
                       {_percentile(0.50, "f.fiscal_year")} AS median_wage,
                       COUNT(*) OVER (PARTITION BY f.fiscal_year) AS n_filings
                FROM filings f
                JOIN locations l ON l.location_id = f.location_id
                WHERE {where}
            )
            SELECT fiscal_year, median_wage, n_filings,
                   ROUND(100.0 * (median_wage - LAG(median_wage) OVER years)
                         / LAG(median_wage) OVER years, 1) AS yoy_pct_change
            FROM per_year
            WINDOW years AS (ORDER BY fiscal_year)
            ORDER BY fiscal_year
            """,
            params,
        )

    def title_search(self, prefix: str | None = "", limit: int = 25) -> list[str]:
        r"""Job titles starting with ``prefix``, most filed first.

        Two dialect changes, both consequences of ``BIN2``:

        The ``LIKE`` needs its own ``COLLATE`` — with a binary collation on
        ``job_title``, typing "data" would match an exact-case prefix and nothing
        else, and the autocomplete would quietly stop working for most of what
        people type. This is the query that runs on every keystroke.

        ``GROUP BY LOWER(job_title)`` cannot then select the bare column, so the
        representative spelling is ``MIN``. Phase 1 lets SQLite pick arbitrarily
        from the group, so **the two backends can return different capitalisations
        of the same title** — a documented divergence, not a porting bug. Either
        spelling finds the same filings, because every query here matches titles
        case-insensitively.

        Outliers are counted here, unlike everywhere else: 37 rows in 850,321
        cannot change an ordering, and excluding them tripled the query time on
        SQLite — which this can least afford.
        """
        frame = self._run(
            rf"""
            SELECT TOP (?) MIN(t.job_title) AS job_title, COUNT(*) AS n
            FROM filings f
            JOIN titles t ON t.title_id = f.title_id
            WHERE t.job_title COLLATE {_CI} LIKE ? + '%' ESCAPE '\'
            GROUP BY LOWER(t.job_title)
            ORDER BY n DESC, MIN(t.job_title)
            """,
            [
                queries._whole_number("limit", limit),
                _escape_like(queries._text("prefix", prefix) or ""),
            ],
        )
        return frame["job_title"].tolist()
