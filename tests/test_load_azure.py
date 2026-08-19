"""Tests for the Azure loader.

Almost all of it runs offline. The parts worth testing are not the round trip —
Step 8 already proves the two backends agree once loaded — but the four places
this module could silently corrupt a load:

* reading the wrong blobs, or reading them without deduplicating;
* sending ``NaN`` into an ``INT`` column;
* clearing the tables in an order the foreign keys reject;
* treating a paused database as a fatal error.

The one live test asserts the row counts the plan names as Step 9's acceptance
criterion, and it is read-only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from conftest import require_module
from src import clean, ingest, load
from src.etl import load_azure

# NOT a module-level pyodbc guard. Most of this file is pure pandas — the
# golden-shape tests above all, which are what protect the refactor — and
# guarding the module would make them vanish on any machine without the ODBC
# driver, which is most machines. Only the tests that actually import pyodbc
# ask for it.


# --------------------------------------------------------------------------
# The frames both backends share
# --------------------------------------------------------------------------


def _frame(rows: int = 5) -> pd.DataFrame:
    """Raw-shaped filings, as the DOL files present them."""
    return pd.DataFrame(
        {
            "CASE_NUMBER": [f"I-200-25001-{i:06d}" for i in range(1, rows + 1)],
            "CASE_STATUS": "Certified",
            "VISA_CLASS": "H-1B",
            "DECISION_DATE": "2025-01-15",
            "RECEIVED_DATE": "2024-11-01",
            # Deliberately NOT in alphabetical order, and neither are the
            # cities below. Ids are assigned in first-seen order; a fixture
            # whose values happen to be sorted cannot tell that apart from
            # alphabetical, and a drift in id assignment would pass unnoticed.
            # Verified: renumbering the lookups alphabetically fails this file.
            "EMPLOYER_NAME": ["ZYLO INC", "ZYLO INC", "ACME", "ACME", "MIDCO"][
                :rows
            ],
            "JOB_TITLE": "Software Engineer",
            "SOC_CODE": "15-1252.00",
            "SOC_TITLE": "Software Developers",
            "WORKSITE_CITY": ["seattle", "seattle", "seattle", "austin", "austin"][:rows],
            "WORKSITE_STATE": ["wa", "wa", "wa", "tx", "tx"][:rows],
            "WAGE_RATE_OF_PAY_FROM": [100_000.0, 120_000.0, 140_000.0, 160_000.0,
                                      180_000.0][:rows],
            "WAGE_RATE_OF_PAY_TO": None,
            "WAGE_UNIT_OF_PAY": "Year",
            "PREVAILING_WAGE": 95_000.0,
            "PW_UNIT_OF_PAY": "Year",
            "FULL_TIME_POSITION": "Y",
        }
    )


#: What ``build_tables`` must produce for :func:`_frame`, written out in full.
#:
#: **Pinned, not derived.** An earlier version of this test built a SQLite
#: database with ``load.build()`` and compared ``build_tables`` against it —
#: which is circular, because ``load.build`` calls ``build_tables``. It compared
#: the function to itself and would have passed no matter how far the extraction
#: drifted.
#:
#: These values are trustworthy because the extraction was checked differentially
#: against the pre-refactor ``load.py`` over 145,099 real filings from two DOL
#: caches: all six tables identical, and the two SQLite files byte-for-byte
#: equal. That check cannot live in the suite — it needs the old code — so its
#: result is pinned here instead.
#:
#: The ids are the point. They are assigned by ``range(len(values))`` in Python,
#: so a change in row order, in ``drop_duplicates``, or in the LOOKUPS order
#: renumbers them, and every foreign key in ``filings`` then means something
#: else while every count stays plausible.
GOLDEN = {
    "employers": {
        "employer_id": [0, 1, 2],
        "employer_name": ["ZYLO", "ACME", "MIDCO"],
        "raw_name_sample": ["ZYLO INC", "ACME", "MIDCO"],
    },
    "occupations": {
        "soc_id": [0],
        "soc_code": ["15-1252"],
        "soc_title": ["Software Developers"],
    },
    "titles": {"title_id": [0], "job_title": ["Software Engineer"]},
    "locations": {
        "location_id": [0, 1],
        "worksite_city": ["Seattle", "Austin"],
        "worksite_state": ["WA", "TX"],
    },
    "visa_classes": {"visa_class_id": [0], "visa_class": ["H-1B"]},
    "filings": {
        "employer_id": [0, 0, 1, 1, 2],
        "soc_id": [0, 0, 0, 0, 0],
        "title_id": [0, 0, 0, 0, 0],
        "location_id": [0, 0, 0, 1, 1],
        "visa_class_id": [0, 0, 0, 0, 0],
        "case_prefix": [200, 200, 200, 200, 200],
        "case_serial": [
            25001000001, 25001000002, 25001000003, 25001000004, 25001000005
        ],
        "annual_wage": [100000, 120000, 140000, 160000, 180000],
        "annual_from": [100000, 120000, 140000, 160000, 180000],
        "annual_to": [None, None, None, None, None],
        "prevailing_wage": [95000, 95000, 95000, 95000, 95000],
        "fiscal_year": [2025, 2025, 2025, 2025, 2025],
        "full_time": [1, 1, 1, 1, 1],
        "withdrawn": [0, 0, 0, 0, 0],
        "is_outlier": [0, 0, 0, 0, 0],
        "pw_outlier": [0, 0, 0, 0, 0],
        "unit_repaired": [0, 0, 0, 0, 0],
        "pw_repaired": [0, 0, 0, 0, 0],
    },
}


@pytest.mark.parametrize("table", list(GOLDEN))
def test_build_tables_matches_the_pinned_shape(table):
    """One definition of the data, and it must not move under either backend."""
    produced = load.build_tables(clean.clean(_frame()))[table]
    expected = pd.DataFrame(GOLDEN[table])

    assert list(produced.columns) == list(expected.columns), table
    pd.testing.assert_frame_equal(
        produced.reset_index(drop=True).astype(object).where(pd.notna(produced), None),
        expected.astype(object).where(pd.notna(expected), None),
        check_dtype=False,
    )


def test_the_sqlite_build_stores_exactly_the_pinned_shape(tmp_path):
    """And the database really does receive it — the other half of the claim.

    Reads the built file back rather than trusting the writer, so a ``to_sql``
    that silently dropped or reordered a column would fail here.
    """
    path, _ = load.build(clean.clean(_frame()), tmp_path / "small.db")
    connection = sqlite3.connect(path)
    try:
        for table, expected in GOLDEN.items():
            stored = pd.read_sql_query(
                f"SELECT {', '.join(expected)} FROM {table}", connection
            )
            assert stored.to_dict("list") == expected, table
    finally:
        connection.close()


def test_build_tables_orders_lookups_before_filings():
    """The foreign keys make the order mandatory, so it is asserted, not assumed."""
    tables = list(load.build_tables(clean.clean(_frame())))
    assert tables[-1] == "filings"
    assert tables[:-1] == [name for name, _, _, _ in load.LOOKUPS]


# --------------------------------------------------------------------------
# Reading the caches
# --------------------------------------------------------------------------


def test_read_cleaned_deduplicates_cases_spanning_two_files(tmp_path):
    """20,873 real cases appear in two files; concatenating alone loads them twice.

    The later decision wins, so the row that survives must be the withdrawal —
    this is the behaviour ``ingest.combine`` owns and the loader must not
    reimplement.
    """
    first = _frame(2)
    second = _frame(2).assign(
        CASE_STATUS="Certified - Withdrawn", DECISION_DATE="2025-06-01"
    )
    paths = []
    for name, frame in (("LCA_a.parquet", first), ("LCA_b.parquet", second)):
        path = tmp_path / name
        frame.astype({c: "string" for c in frame.columns if frame[c].dtype == object}
                    ).to_parquet(path, index=False)
        paths.append(path)

    cleaned = load_azure.read_cleaned(paths)
    assert len(cleaned) == 2, "a case in two files must load once"
    assert cleaned["case_status"].eq("Certified - Withdrawn").all()


def test_download_caches_ignores_blobs_that_are_not_dol_data(monkeypatch, tmp_path):
    """``curated`` artifacts and probe files must not be read as source data."""
    monkeypatch.setattr(
        load_azure.blob,
        "list_raw",
        lambda: [("LCA_Disclosure_Data_FY2024_Q1.parquet", 1), ("probe.parquet", 1)],
    )
    seen = []
    monkeypatch.setattr(
        load_azure.blob,
        "download_raw",
        lambda name, dest: seen.append(name) or Path(dest) / name,
    )
    load_azure.download_caches(tmp_path)
    assert seen == ["LCA_Disclosure_Data_FY2024_Q1.parquet"]


def test_download_caches_explains_an_empty_container(monkeypatch, tmp_path):
    """The 90-day lifecycle rule empties ``raw``; that reads as a broken upload."""
    monkeypatch.setattr(load_azure.blob, "list_raw", lambda: [])
    with pytest.raises(FileNotFoundError, match="lifecycle rule"):
        load_azure.download_caches(tmp_path)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


class _Cursor:
    """Records what it was asked to do, and nothing else."""

    def __init__(self):
        self.statements: list[str] = []
        self.batches: list[list[tuple]] = []
        self.fast_executemany = False

    def execute(self, statement, *_):
        self.statements.append(statement)

    def executemany(self, statement, rows):
        self.statements.append(statement)
        self.batches.append(list(rows))


def test_nulls_become_none_not_nan():
    """``NaN`` reaching an ``INT`` column fails the batch, naming no row.

    ``annual_to`` is NULL for every filing that gave a single wage rather than a
    band, which is most of them, so this is the common path and not an edge.
    """
    frame = pd.DataFrame({"a": pd.array([1, None], dtype="Int64"), "b": [1.5, None]})
    rows = next(load_azure._batches(frame))
    assert rows[0] == (1, 1.5)
    assert rows[1] == (None, None)
    assert not any(isinstance(v, float) and v != v for row in rows for v in row)


@pytest.mark.parametrize("rows, size, expected", [(0, 10, []), (10, 10, [10]),
                                                  (25, 10, [10, 10, 5])])
def test_batches_cover_the_frame_exactly(rows, size, expected):
    """An exact multiple must not emit a trailing empty batch."""
    frame = pd.DataFrame({"a": range(rows)})
    assert [len(b) for b in load_azure._batches(frame, size)] == expected


def test_insert_names_its_columns_and_turns_on_fast_executemany():
    """A positional INSERT would transpose data silently if the schema moved.

    And without ``fast_executemany`` this is one round trip per row: 850,321 of
    them, which burns the serverless compute grant before it finishes.
    """
    cursor = _Cursor()
    frame = pd.DataFrame({"visa_class_id": [0, 1], "visa_class": ["H-1B", "E-3"]})
    written = load_azure.insert(cursor, "visa_classes", frame, size=1)

    assert written == 2
    assert cursor.fast_executemany is True
    assert cursor.statements[0] == (
        "INSERT INTO dbo.visa_classes (visa_class_id, visa_class) VALUES (?, ?)"
    )
    assert [len(b) for b in cursor.batches] == [1, 1]


def test_clear_empties_children_before_parents():
    """Deleting a lookup while ``filings`` still references it is a FK error."""
    cursor = _Cursor()
    load_azure.clear(cursor)

    assert cursor.statements[0] == "TRUNCATE TABLE dbo.filings"
    cleared = [s.split()[-1] for s in cursor.statements[1:]]
    assert cleared == [f"dbo.{name}" for name, _, _, _ in reversed(load.LOOKUPS)]


def test_verify_refuses_a_short_load():
    """"It ran without raising" is not the same as "it loaded everything"."""
    with pytest.raises(RuntimeError, match="wrote 5 filings but cleaned 10"):
        load_azure._verify({"filings": 5}, 10)


# --------------------------------------------------------------------------
# The paused database
# --------------------------------------------------------------------------


def test_connect_awake_waits_out_a_resume(monkeypatch):
    """The most likely reason this job fails, and it is not a fault.

    Exercised through ``load_azure.connect_awake``, which is the backend's —
    the retry moved to :mod:`src.db.azure_impl` so the dashboard gets it too.
    """
    require_module("pyodbc", "ODBC driver not installed")
    import pyodbc

    attempts = {"n": 0}

    def flaky(*_args):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise pyodbc.OperationalError("HYT00", "Login timeout expired")
        return "connection"

    monkeypatch.setattr("src.db.azure_impl.connect", flaky)

    assert load_azure.connect_awake(wait=0, echo=lambda *_: None) == "connection"
    assert attempts["n"] == 3


def test_connect_awake_gives_up_and_says_what_it_waited_for(monkeypatch):
    require_module("pyodbc", "ODBC driver not installed")
    import pyodbc

    def always_asleep(*_args):
        raise pyodbc.OperationalError("HYT00", "Login timeout expired")

    monkeypatch.setattr("src.db.azure_impl.connect", always_asleep)

    with pytest.raises(RuntimeError, match="did not resume within"):
        load_azure.connect_awake(timeout=0, wait=0, echo=lambda *_: None)


@pytest.mark.parametrize(
    "sqlstate, message",
    [
        ("28000", "Login failed for user '<token-identified principal>'"),
        ("01000", "Can't open lib 'ODBC Driver 18 for SQL Server'"),
    ],
)
def test_connect_awake_does_not_retry_a_fatal_failure(monkeypatch, sqlstate, message):
    """The boundary this function exists to draw, tested where it actually is.

    An earlier version of this test raised ``ValueError``, which was never a
    ``pyodbc.Error`` and so passed no matter what the retry filter did — it
    tested nothing. Both SQLSTATEs below *are* ``pyodbc.Error``, which is why
    catching that class whole was wrong: a revoked role assignment (28000,
    raised in 1.5 s) was waited on for the full timeout and then reported as
    ``database did not resume``.
    """
    require_module("pyodbc", "ODBC driver not installed")
    import pyodbc

    attempts = {"n": 0}

    def fatal(*_args):
        attempts["n"] += 1
        raise pyodbc.Error(sqlstate, message)

    monkeypatch.setattr("src.db.azure_impl.connect", fatal)
    with pytest.raises(pyodbc.Error, match=sqlstate):
        load_azure.connect_awake(wait=0, echo=lambda *_: None)
    assert attempts["n"] == 1, "a fatal failure must not be retried"


def test_connect_awake_does_not_retry_a_credential_failure(monkeypatch):
    """A bad token never reaches pyodbc — it fails while fetching the token."""

    def broken(*_args):
        raise ValueError("no credential")

    monkeypatch.setattr("src.db.azure_impl.connect", broken)
    with pytest.raises(ValueError, match="no credential"):
        load_azure.connect_awake(echo=lambda *_: None)


def test_a_resuming_database_is_recognised_by_its_message():
    """Error 40613 arrives as text, not as a SQLSTATE of its own."""
    require_module("pyodbc", "ODBC driver not installed")
    import pyodbc

    from src.db.azure_impl import _is_resuming

    assert _is_resuming(pyodbc.Error("HYT00", "Login timeout expired"))
    assert _is_resuming(
        pyodbc.Error("42000", "Database 'sqldb-h1b' is not currently available")
    )
    assert not _is_resuming(pyodbc.Error("28000", "Login failed for user"))
    assert not _is_resuming(pyodbc.Error("01000", "Can't open lib"))


# --------------------------------------------------------------------------
# The live acceptance criterion
# --------------------------------------------------------------------------


@pytest.mark.azure
def test_azure_holds_the_full_dataset():
    """Plan Step 9's acceptance criterion, as a test rather than a one-off check.

    ``locations`` is the number that proves the collation fix held: under the
    default collation the lookups collapse, and under ``BIN2`` alone the titles
    do. Read-only.
    """
    from conftest import unavailable

    try:
        connection = load_azure.connect_awake(echo=lambda *_: None)
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot check"
        unavailable(f"Azure SQL unreachable: {exc}")

    expected = {
        "filings": 850_321,
        "employers": 43_573,
        "titles": 123_990,
        "locations": 8_570,
        "occupations": 63,
        "visa_classes": 4,
    }
    try:
        cursor = connection.cursor()
        actual = {
            table: cursor.execute(f"SELECT COUNT(*) FROM dbo.{table}").fetchval()
            for table in expected
        }
    finally:
        connection.close()

    assert actual == expected
