"""The interface both backends implement.

Plan Step 8. Phase 1's :mod:`src.queries` is seven module-level functions; this
is the same seven as methods, so ``app.py`` can be handed either a SQLite or an
Azure SQL implementation and not know which it has.

**Seven, not five.** ``wage_distribution`` and ``fiscal_years`` were added during
Phase 1's own Step 8, because the dashboard needed a histogram and a year picker
the other five cannot feed. ``app.py`` calls all seven. A Protocol listing five
type-checks, reviews cleanly, and fails at runtime on the two it forgot — so the
count is asserted in :mod:`tests.test_db`.

**``db`` becomes construction state, not an argument.** Every Phase 1 function
takes ``db: Path | None`` so the tests can point at a small database. On an
interface that would be a SQLite detail leaking into a method signature Azure has
no use for, so it moves into the constructor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

__all__ = ["Backend", "METHODS"]

# Asserted against the Protocol in the tests. Written out rather than derived so
# that deleting a method from the Protocol fails a test instead of silently
# shrinking the interface — the exact failure this list exists to prevent.
METHODS = (
    "salary_percentiles",
    "wage_distribution",
    "top_employers",
    "salary_by_city",
    "salary_trend",
    "title_search",
    "fiscal_years",
)


@runtime_checkable
class Backend(Protocol):
    """Every question the dashboard asks, independent of which database answers.

    ``runtime_checkable`` so the tests can assert an implementation satisfies it.
    Note that this only checks method *names* exist, not their signatures — which
    is precisely why the equality tests in ``tests/test_db.py`` compare real
    output from both backends rather than trusting ``isinstance``.
    """

    #: What the dashboard opens on.
    #:
    #: Not cosmetic. Unfiltered, every query below ranks all 850,321 rows and
    #: takes 0.7-1.6 s on SQLite, where filtered they answer in 8-130 ms. Four
    #: covering indexes on ``annual_wage`` were measured at +58 MB for no
    #: improvement, because SQLite sorts for a window function whether or not an
    #: index could supply the order. Azure SQL's ``INCLUDE`` columns may change
    #: that and it is worth measuring after Step 9 — until then, both backends
    #: keep the unfiltered path off the first screen a visitor sees.
    DEFAULT_JOB_TITLE: str

    #: What ``app.py`` must catch around a query.
    #:
    #: Phase 1 catches ``sqlite3.Error``. Hardcoding that in the dashboard would
    #: mean an Azure failure — a paused database, an expired token, a dropped
    #: connection — escapes as an unhandled exception and Streamlit renders a
    #: traceback to the visitor. Each backend names its own.
    TROUBLE: tuple[type[BaseException], ...]

    def salary_percentiles(
        self,
        job_title: str | None = None,
        city: str | None = None,
        state: str | None = None,
        fiscal_year: int | None = None,
        include_outliers: bool = False,
    ) -> pd.DataFrame:
        """p25, p50, p75 and the filing count for one slice. Always one row.

        An empty slice returns NULL percentiles and ``n_filings`` 0 rather than
        no rows, so the dashboard has something to render either way.
        """
        ...

    def wage_distribution(
        self,
        job_title: str | None = None,
        city: str | None = None,
        state: str | None = None,
        fiscal_year: int | None = None,
        include_outliers: bool = False,
        bin_width: int = 10_000,
    ) -> pd.DataFrame:
        """Filing counts per wage band. Columns ``bin_floor``, ``n_filings``.

        A band with no filings is absent rather than zero.
        """
        ...

    def top_employers(
        self,
        job_title: str | None = None,
        city: str | None = None,
        limit: int = 20,
    ) -> pd.DataFrame:
        """Employers filing most often for this slice, with their median wage."""
        ...

    def salary_by_city(
        self,
        job_title: str | None = None,
        min_filings: int = 10,
    ) -> pd.DataFrame:
        """Median wage per worksite, for cities with enough filings to mean anything."""
        ...

    def salary_trend(
        self,
        job_title: str | None = None,
        city: str | None = None,
    ) -> pd.DataFrame:
        """Median wage per fiscal year, with the year-over-year change.

        ``yoy_pct_change`` is NULL for the earliest year — ``LAG`` behaving
        correctly, not missing data.
        """
        ...

    def title_search(self, prefix: str | None = "", limit: int = 25) -> list[str]:
        """Job titles starting with ``prefix``, most filed first.

        Returns a list, not a DataFrame — ``app.py`` feeds it straight into a
        ``selectbox``.
        """
        ...

    def fiscal_years(self) -> list[int]:
        """Every fiscal year in the data, newest first. A list, as above."""
        ...
