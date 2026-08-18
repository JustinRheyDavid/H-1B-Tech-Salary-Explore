"""The SQLite backend: a thin class over Phase 1's :mod:`src.queries`.

Deliberately thin. Every line of SQL stays in ``src/queries.py`` — it is the
exhibit the README points a reviewer at for the SQL certificate, ``app.py``
imports it, and ``tests/test_queries.py`` tests it directly. Moving that SQL in
here would churn three things to no benefit and would mean the Phase 1 suite was
no longer testing the code Phase 1 ships.

So this adapter does exactly one thing: turn seven module-level functions that
each take ``db=`` into seven methods that share it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from src import queries

__all__ = ["SQLiteBackend"]


class SQLiteBackend:
    """Phase 1's queries, behind the :class:`~src.db.base.Backend` interface."""

    DEFAULT_JOB_TITLE = queries.DEFAULT_JOB_TITLE

    # Phase 1's error path, unchanged. sqlite3.Error is the base of every
    # exception the driver raises, so this catches a missing file, a corrupt
    # database and a bad query alike.
    TROUBLE: tuple[type[BaseException], ...] = (sqlite3.Error,)

    def __init__(self, db: Path | None = None) -> None:
        """``db=None`` means :data:`src.load.DB_PATH`, exactly as Phase 1 means it."""
        self.db = db

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SQLiteBackend(db={self.db!r})"

    def salary_percentiles(
        self,
        job_title: str | None = None,
        city: str | None = None,
        state: str | None = None,
        fiscal_year: int | None = None,
        include_outliers: bool = False,
    ) -> pd.DataFrame:
        return queries.salary_percentiles(
            job_title, city, state, fiscal_year, include_outliers, db=self.db
        )

    def wage_distribution(
        self,
        job_title: str | None = None,
        city: str | None = None,
        state: str | None = None,
        fiscal_year: int | None = None,
        include_outliers: bool = False,
        bin_width: int = 10_000,
    ) -> pd.DataFrame:
        return queries.wage_distribution(
            job_title, city, state, fiscal_year, include_outliers, bin_width, db=self.db
        )

    def top_employers(
        self,
        job_title: str | None = None,
        city: str | None = None,
        limit: int = 20,
    ) -> pd.DataFrame:
        return queries.top_employers(job_title, city, limit, db=self.db)

    def salary_by_city(
        self,
        job_title: str | None = None,
        min_filings: int = 10,
    ) -> pd.DataFrame:
        return queries.salary_by_city(job_title, min_filings, db=self.db)

    def salary_trend(
        self,
        job_title: str | None = None,
        city: str | None = None,
    ) -> pd.DataFrame:
        return queries.salary_trend(job_title, city, db=self.db)

    def title_search(self, prefix: str | None = "", limit: int = 25) -> list[str]:
        return queries.title_search(prefix, limit, db=self.db)

    def fiscal_years(self) -> list[int]:
        return queries.fiscal_years(db=self.db)
