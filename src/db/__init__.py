"""Pick a backend from the environment.

Plan Step 8. ``app.py`` asks for a backend and gets whichever one the deployment
configured, without importing either implementation by name.

**The default is ``sqlite``, and that matters more than it looks.** Phase 1 stays
deployed on Streamlit Cloud (assumption B6) and its tests run in CI with no cloud
dependency at all. If the default were ``azure``, a fresh clone would fail on an
import of ``pyodbc`` that most machines cannot satisfy, and a green Phase 1 suite
would start depending on a database being awake.

``azure_impl`` is imported inside :func:`get_backend`, never at module level, for
the same reason — importing this package must not require the Azure SDK or an
ODBC driver.
"""

from __future__ import annotations

import os

from src.db.base import METHODS, Backend

__all__ = ["Backend", "METHODS", "get_backend", "BACKEND_ENV"]

BACKEND_ENV = "DB_BACKEND"


def get_backend(name: str | None = None) -> Backend:
    """Return the configured backend. ``DB_BACKEND``: ``sqlite`` (default) | ``azure``.

    An unknown value raises rather than falling back to SQLite. A typo'd
    ``DB_BACKEND=azue`` in a container's environment would otherwise start the
    dashboard quietly reading a SQLite file that is not there in Azure, and the
    symptom — an empty dashboard — says nothing about the cause.
    """
    choice = (name or os.environ.get(BACKEND_ENV) or "sqlite").strip().lower()

    if choice == "sqlite":
        from src.db.sqlite_impl import SQLiteBackend

        return SQLiteBackend()

    if choice == "azure":
        from src.db.azure_impl import AzureBackend

        return AzureBackend()

    raise ValueError(
        f"{BACKEND_ENV} must be 'sqlite' or 'azure', got {choice!r}"
    )
