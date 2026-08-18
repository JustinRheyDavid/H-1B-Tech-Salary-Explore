"""Tests for the backend split.

Three groups, and only the first two run without a cloud account.

**Interface** — that the Protocol lists all seven methods and both backends
implement them. A Protocol listing five type-checks, reviews cleanly, and fails
at runtime on the two it forgot, which is exactly what happened to an earlier
draft of the plan.

**Selection** — that ``get_backend()`` reads ``DB_BACKEND``, defaults to SQLite,
and refuses a typo rather than silently falling back.

**Equality** — marked ``azure``. Plan Step 8's real acceptance criterion: the two
backends must return results *equal*, not merely identically shaped. That is only
possible because Phase 1's hand-rolled percentiles interpolate linearly between
the two rows either side of ``1 + fraction * (n - 1)`` — which is
``PERCENTILE_CONT``'s definition exactly. Whether Azure's *implementation*
matches that definition is what these tests settle.

The comparison is read-only. It mirrors whatever Azure currently holds into a
temporary SQLite database and asks both backends the same questions, so it needs
no fixture data of its own and cannot corrupt the real tables. It skips when
Azure is unreachable or empty.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src import clean, load, queries
from src.db import BACKEND_ENV, METHODS, Backend, get_backend
from src.db.sqlite_impl import SQLiteBackend

# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------


def test_the_protocol_lists_seven_methods():
    """Five would compile and fail at runtime on the two it forgot.

    ``wage_distribution`` and ``fiscal_years`` were added during Phase 1's own
    Step 8 because the dashboard needed a histogram and a year picker the other
    five cannot feed.
    """
    assert len(METHODS) == 7
    assert set(METHODS) == {
        "salary_percentiles",
        "wage_distribution",
        "top_employers",
        "salary_by_city",
        "salary_trend",
        "title_search",
        "fiscal_years",
    }


def test_every_protocol_method_exists_on_the_protocol():
    for name in METHODS:
        assert hasattr(Backend, name), name


def test_sqlite_backend_satisfies_the_protocol():
    backend = SQLiteBackend()
    assert isinstance(backend, Backend)
    for name in METHODS:
        assert callable(getattr(backend, name)), name


def test_the_interface_covers_everything_app_py_calls():
    """The interface exists to serve ``app.py``; anything it calls must be on it.

    Guards the failure the plan hit: a Protocol that looks complete because it
    was written from a list rather than from the caller.
    """
    source = (Path(__file__).resolve().parent.parent / "app.py").read_text()
    called = {name for name in queries.__all__ if f"queries.{name}" in source}
    interface = set(METHODS) | {"DEFAULT_JOB_TITLE"}
    assert called <= interface, f"app.py uses {called - interface}, absent from the Backend"


def test_azure_backend_satisfies_the_protocol():
    """The half of the interface that was untested, and the half that motivated it.

    ``TROUBLE`` exists because hardcoding ``sqlite3.Error`` in ``app.py`` would
    let an Azure failure — paused database, expired token — escape as an
    unhandled exception and render a traceback to a visitor. The SQLite side of
    that was asserted and the Azure side was not, which is backwards.

    Offline: it imports the module and reads attributes, and never opens a
    connection. It needs ``pyodbc`` importable only because ``TROUBLE`` names
    ``pyodbc.Error``.
    """
    pytest.importorskip("pyodbc", reason="ODBC driver not installed")
    import pyodbc
    from azure.core.exceptions import ClientAuthenticationError

    from src.db.azure_impl import AzureBackend

    backend = AzureBackend()
    assert isinstance(backend, Backend)
    for name in METHODS:
        assert callable(getattr(backend, name)), name
    assert backend.DEFAULT_JOB_TITLE == queries.DEFAULT_JOB_TITLE
    assert pyodbc.Error in backend.TROUBLE
    assert ClientAuthenticationError in backend.TROUBLE


def test_both_backends_declare_the_non_method_members():
    """``DEFAULT_JOB_TITLE`` and ``TROUBLE`` are interface members too.

    ``TROUBLE`` especially: without it ``app.py`` hardcodes ``sqlite3.Error``,
    and every Azure failure — paused database, expired token — escapes as an
    unhandled exception that Streamlit renders as a traceback to the visitor.
    """
    backend = SQLiteBackend()
    assert backend.DEFAULT_JOB_TITLE == queries.DEFAULT_JOB_TITLE
    assert sqlite3.Error in backend.TROUBLE


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_default_is_sqlite(monkeypatch):
    """A fresh clone with no environment must not need pyodbc or a network."""
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    assert isinstance(get_backend(), SQLiteBackend)


def test_environment_selects_the_backend(monkeypatch):
    monkeypatch.setenv(BACKEND_ENV, "sqlite")
    assert isinstance(get_backend(), SQLiteBackend)


@pytest.mark.parametrize("value", ["SQLite", " sqlite ", "SQLITE"])
def test_backend_name_is_case_and_space_insensitive(monkeypatch, value):
    monkeypatch.setenv(BACKEND_ENV, value)
    assert isinstance(get_backend(), SQLiteBackend)


def test_an_unknown_backend_raises_rather_than_falling_back(monkeypatch):
    """``DB_BACKEND=azue`` in a container must not quietly serve SQLite.

    Falling back would start the dashboard reading a SQLite file that is not
    present in Azure, and the symptom — an empty dashboard — says nothing about
    the cause.
    """
    monkeypatch.setenv(BACKEND_ENV, "azue")
    with pytest.raises(ValueError, match="azue"):
        get_backend()


def test_importing_the_package_does_not_require_the_azure_sdk():
    """``src.db`` is imported by ``app.py`` on both deployments.

    If ``azure_impl`` were imported at module level, a machine with no ODBC
    driver — which is most machines — could not run the Phase 1 dashboard.

    Runs in a **fresh interpreter**, and that is the point. An earlier version
    asserted ``"azure_impl" not in dir(src.db)`` in-process, which passed only
    while no earlier test in the session had imported the submodule — importing
    it binds it as an attribute of the parent package. That made the test a
    statement about test ordering rather than about the import graph, and it
    broke the moment a conformance test was added above it. A subprocess tests
    the property itself.
    """
    probe = (
        "import sys; import src.db; "
        "sys.exit(1 if 'src.db.azure_impl' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing src.db pulled in azure_impl\n{result.stdout}{result.stderr}"
    )


# --------------------------------------------------------------------------
# The SQLite adapter delegates, it does not reimplement
# --------------------------------------------------------------------------


@pytest.fixture
def small_db(tmp_path):
    """A handful of filings, built exactly as the Phase 1 suite builds them.

    Deliberately NOT ``data/h1b.db``. These tests prove the adapter *delegates*
    — that it adds nothing and changes nothing — and a five-row database proves
    that as well as an 850,321-row one, while staying independent of whether the
    real database has been built.

    An earlier draft pointed them at the real database, and an earlier version of
    this docstring blamed ``top_employers`` for "running for minutes". That was
    wrong, and measurement is the reason it is corrected here rather than
    repeated: against all 850,321 rows it takes 0.11 s filtered, 0.25 s for a
    city, 0.63 s unfiltered, through the adapter or directly. The apparent hang
    came from timing it in a *backgrounded* process, which absorbs machine
    suspend into wall clock — the tell was that the reported figures, 42 minutes
    and 21 hours, were human-shaped rather than query-shaped, while the four
    calls either side of it in the same run read 0.05 s.
    """
    frame = pd.DataFrame(
        {
            "CASE_NUMBER": [f"I-200-25001-{i:06d}" for i in range(1, 6)],
            "CASE_STATUS": "Certified",
            "VISA_CLASS": "H-1B",
            "DECISION_DATE": "2025-01-15",
            "RECEIVED_DATE": ["2024-11-01", "2024-11-01", "2023-11-01",
                              "2023-11-01", "2023-11-01"],
            "EMPLOYER_NAME": ["ACME INC", "ACME INC", "GLOBEX", "GLOBEX", "INITECH"],
            "JOB_TITLE": queries.DEFAULT_JOB_TITLE,
            "SOC_CODE": "15-1252.00",
            "SOC_TITLE": "Software Developers",
            "WORKSITE_CITY": ["austin", "austin", "austin", "seattle", "seattle"],
            "WORKSITE_STATE": ["tx", "tx", "tx", "wa", "wa"],
            "WAGE_RATE_OF_PAY_FROM": [100_000.0, 120_000.0, 140_000.0,
                                      160_000.0, 180_000.0],
            "WAGE_RATE_OF_PAY_TO": None,
            "WAGE_UNIT_OF_PAY": "Year",
            "PREVAILING_WAGE": 95_000.0,
            "PW_UNIT_OF_PAY": "Year",
            "FULL_TIME_POSITION": "Y",
        }
    )
    path, _ = load.build(clean.clean(frame), tmp_path / "small.db")
    return Path(path)


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("salary_percentiles", {"job_title": queries.DEFAULT_JOB_TITLE}),
        ("salary_percentiles", {}),
        ("wage_distribution", {"job_title": queries.DEFAULT_JOB_TITLE}),
        ("top_employers", {"job_title": queries.DEFAULT_JOB_TITLE, "limit": 5}),
        ("salary_by_city", {"min_filings": 1}),
        ("salary_trend", {"job_title": queries.DEFAULT_JOB_TITLE}),
    ],
)
def test_adapter_returns_exactly_what_the_function_returns(small_db, method, kwargs):
    """The adapter must be transparent — every line of SQL stays in queries.py."""
    through_backend = getattr(SQLiteBackend(db=small_db), method)(**kwargs)
    directly = getattr(queries, method)(**kwargs, db=small_db)
    pd.testing.assert_frame_equal(through_backend, directly)


@pytest.mark.parametrize("method", ["title_search", "fiscal_years"])
def test_adapter_returns_lists_for_the_two_that_are_lists(small_db, method):
    """``app.py`` feeds both straight into a selectbox; a DataFrame would break it."""
    result = getattr(SQLiteBackend(db=small_db), method)()
    assert isinstance(result, list)
    assert result == getattr(queries, method)(db=small_db)


def test_adapter_passes_the_database_path_through(small_db):
    """``db`` moved from an argument to construction state; it must still arrive."""
    assert SQLiteBackend(db=small_db).fiscal_years() == queries.fiscal_years(db=small_db)


# --------------------------------------------------------------------------
# Equality between backends
# --------------------------------------------------------------------------


def _mirror_azure_into_sqlite(azure, destination: Path) -> int:
    """Copy Azure's current contents into a fresh SQLite database.

    Read-only against Azure. Comparing the two backends needs them to hold the
    same rows, and mirroring is the only way to guarantee that without writing
    to the real tables — which after Step 9 hold 850,321 rows that no test has
    any business touching.
    """
    connection = sqlite3.connect(destination)
    connection.executescript(load.SCHEMA)
    source = azure._run("SELECT 1 AS ok", [])  # cheap reachability probe
    assert not source.empty

    tables = {
        "employers": ["employer_id", "employer_name", "raw_name_sample"],
        "occupations": ["soc_id", "soc_code", "soc_title"],
        "titles": ["title_id", "job_title"],
        "locations": ["location_id", "worksite_city", "worksite_state"],
        "visa_classes": ["visa_class_id", "visa_class"],
        "filings": [
            "case_serial", "case_prefix", "employer_id", "soc_id", "title_id",
            "location_id", "visa_class_id", "annual_wage", "annual_from",
            "annual_to", "prevailing_wage", "fiscal_year", "full_time",
            "withdrawn", "is_outlier", "pw_outlier", "unit_repaired", "pw_repaired",
        ],
    }
    filings = 0
    for table, columns in tables.items():
        frame = azure._run(f"SELECT {', '.join(columns)} FROM {table}", [])
        frame.to_sql(table, connection, if_exists="append", index=False)
        if table == "filings":
            filings = len(frame)
    connection.commit()
    connection.close()
    return filings


#: Set ``REQUIRE_AZURE=1`` to turn every skip below into a failure.
#:
#: The skips exist so a clone with no Azure account still runs green, and that
#: is right for a contributor. It is wrong for the one CI job whose entire
#: purpose is to exercise Azure: a paused database, an expired federated
#: credential or a missing ODBC driver would each report success while testing
#: nothing. This flag is what lets the same tests serve both.
REQUIRE_AZURE = "REQUIRE_AZURE"


def _azure_is_mandatory() -> bool:
    return os.environ.get(REQUIRE_AZURE, "").strip().lower() in {"1", "true", "yes"}


def _unavailable(reason: str):
    """Skip, or fail if the environment said these tests are mandatory."""
    if _azure_is_mandatory():
        pytest.fail(f"{REQUIRE_AZURE} is set, but the Azure tests cannot run: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def backends(tmp_path_factory):
    """An Azure backend and a SQLite backend holding identical rows."""
    try:
        import pyodbc  # noqa: F401
    except ImportError as exc:
        _unavailable(f"ODBC driver not installed: {exc}")
    from src.db.azure_impl import AzureBackend

    azure = AzureBackend()
    mirror = tmp_path_factory.mktemp("mirror") / "mirror.db"
    try:
        rows = _mirror_azure_into_sqlite(azure, mirror)
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot compare"
        _unavailable(f"Azure unreachable: {exc}")
    if rows == 0:
        _unavailable("Azure filings table is empty; load it before comparing")
    return azure, SQLiteBackend(db=mirror)


@pytest.mark.azure
def test_percentile_cont_matches_phase_1s_interpolation(backends):
    """The claim the whole port rests on, checked against Azure itself.

    Phase 1 interpolates between the rows either side of ``1 + f * (n - 1)``.
    ``PERCENTILE_CONT`` is documented as the same definition. This runs both
    expressions over the same rows *inside Azure* and requires them to agree
    exactly — if they ever diverge, every median on the dashboard is wrong by an
    amount nobody would notice.
    """
    azure, _ = backends
    frame = azure._run(
        """
        WITH ranked AS (
            SELECT annual_wage,
                   ROW_NUMBER() OVER (ORDER BY annual_wage) AS position,
                   COUNT(*)     OVER ()                     AS n
            FROM filings WHERE annual_wage IS NOT NULL AND is_outlier = 0
        )
        SELECT
            (SELECT SUM(annual_wage
                        * CASE WHEN 1.0 - ABS(position - (1 + 0.5 * (n - 1))) > 0
                               THEN 1.0 - ABS(position - (1 + 0.5 * (n - 1))) ELSE 0.0 END)
             FROM ranked) AS hand_rolled,
            (SELECT DISTINCT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY annual_wage) OVER ()
             FROM ranked) AS native
        """,
        [],
    )
    assert frame["hand_rolled"].iloc[0] == pytest.approx(frame["native"].iloc[0], abs=1e-6)


@pytest.mark.azure
@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("salary_percentiles", {"job_title": queries.DEFAULT_JOB_TITLE}),
        ("salary_percentiles", {"job_title": queries.DEFAULT_JOB_TITLE, "fiscal_year": 2025}),
        ("salary_percentiles", {"job_title": "no such title anywhere"}),
        ("salary_percentiles", {"job_title": queries.DEFAULT_JOB_TITLE, "include_outliers": True}),
        ("wage_distribution", {"job_title": queries.DEFAULT_JOB_TITLE}),
        ("wage_distribution", {"job_title": queries.DEFAULT_JOB_TITLE, "bin_width": 25_000}),
        ("salary_trend", {"job_title": queries.DEFAULT_JOB_TITLE}),
        ("salary_by_city", {"job_title": queries.DEFAULT_JOB_TITLE, "min_filings": 50}),
        ("top_employers", {"job_title": queries.DEFAULT_JOB_TITLE, "limit": 10}),
    ],
)
def test_the_two_backends_agree(backends, method, kwargs):
    """Equal, not merely identically shaped. Plan Step 8's acceptance criterion."""
    azure, lite = backends
    left = getattr(azure, method)(**kwargs)
    right = getattr(lite, method)(**kwargs)

    assert list(left.columns) == list(right.columns), method
    assert len(left) == len(right), f"{method}: {len(left)} rows vs {len(right)}"
    pd.testing.assert_frame_equal(
        left.reset_index(drop=True),
        right.reset_index(drop=True),
        check_dtype=False,   # pyodbc returns Decimal/float where sqlite3 returns int
        rtol=0, atol=1e-6,   # but the VALUES must match, not merely be close
    )


@pytest.mark.azure
def test_fiscal_years_agree(backends):
    azure, lite = backends
    assert azure.fiscal_years() == lite.fiscal_years()


@pytest.mark.azure
@pytest.mark.parametrize("prefix", ["", "soft", "SOFT", "data"])
def test_title_search_agrees_case_insensitively(backends, prefix):
    """Compared case-folded, because the representative spelling legitimately differs.

    Phase 1 selects a bare ``job_title`` while grouping on ``lower(job_title)``,
    so SQLite picks an arbitrary member of the group; T-SQL forbids that and the
    port uses ``MIN``. Either spelling finds the same filings — every query here
    matches titles case-insensitively — so the divergence is cosmetic. Comparing
    the folded forms tests what actually matters: the same set, in the same order.
    """
    azure, lite = backends
    assert [t.lower() for t in azure.title_search(prefix, 25)] == [
        t.lower() for t in lite.title_search(prefix, 25)
    ]


@pytest.mark.azure
def test_azure_escapes_the_bracket_that_sqlite_does_not(backends):
    """945 titles contain ``[``, which is a character class in T-SQL only.

    Unescaped, a prefix reaching one of them is read as a pattern rather than as
    text and the two backends return different rows for the same keystrokes.
    """
    from src.db import azure_impl

    assert azure_impl._escape_like("Engineer [Senior]") == r"Engineer \[Senior]"
    assert queries._escape_like("Engineer [Senior]") == "Engineer [Senior]"

    azure, lite = backends
    assert azure.title_search("[", 25) == lite.title_search("[", 25)
