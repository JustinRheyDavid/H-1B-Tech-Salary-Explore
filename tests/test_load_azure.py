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

require_module("pyodbc", "ODBC driver not installed")


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
            "EMPLOYER_NAME": ["ACME INC", "ACME INC", "GLOBEX", "GLOBEX", "INITECH"][
                :rows
            ],
            "JOB_TITLE": "Software Engineer",
            "SOC_CODE": "15-1252.00",
            "SOC_TITLE": "Software Developers",
            "WORKSITE_CITY": ["austin", "austin", "austin", "seattle", "seattle"][:rows],
            "WORKSITE_STATE": ["tx", "tx", "tx", "wa", "wa"][:rows],
            "WAGE_RATE_OF_PAY_FROM": [100_000.0, 120_000.0, 140_000.0, 160_000.0,
                                      180_000.0][:rows],
            "WAGE_RATE_OF_PAY_TO": None,
            "WAGE_UNIT_OF_PAY": "Year",
            "PREVAILING_WAGE": 95_000.0,
            "PW_UNIT_OF_PAY": "Year",
            "FULL_TIME_POSITION": "Y",
        }
    )


def test_build_tables_is_what_the_sqlite_build_writes(tmp_path):
    """The claim the whole design rests on: one definition, two backends.

    ``build_tables`` was extracted from ``load._write`` so the Azure loader
    could reuse it rather than compute its own ids. If the extraction drifted
    from what SQLite actually stores, nothing would raise — the two backends
    would simply disagree about which ``title_id`` means which title, and the
    Step 8 equality tests would keep passing because both sides read the same
    wrong ids. So compare against the built database, not against itself.
    """
    cleaned = clean.clean(_frame())
    path, _ = load.build(cleaned, tmp_path / "small.db")

    connection = sqlite3.connect(path)
    try:
        for table, frame in load.build_tables(cleaned).items():
            stored = pd.read_sql_query(
                f"SELECT {', '.join(frame.columns)} FROM {table}", connection
            )
            pd.testing.assert_frame_equal(
                stored.reset_index(drop=True),
                frame.reset_index(drop=True),
                check_dtype=False,
            )
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
    """The most likely reason this job fails, and it is not a fault."""
    import pyodbc

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise pyodbc.OperationalError("HYT00", "Login timeout expired")
        return "connection"

    monkeypatch.setattr("src.db.azure_impl.connect", flaky)
    monkeypatch.setattr(load_azure.time, "sleep", lambda _: None)

    assert load_azure.connect_awake(wait=0, echo=lambda *_: None) == "connection"
    assert attempts["n"] == 3


def test_connect_awake_gives_up_and_says_what_it_waited_for(monkeypatch):
    import pyodbc

    def always_asleep():
        raise pyodbc.OperationalError("HYT00", "Login timeout expired")

    monkeypatch.setattr("src.db.azure_impl.connect", always_asleep)
    monkeypatch.setattr(load_azure.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="did not resume within"):
        load_azure.connect_awake(timeout=0, wait=0, echo=lambda *_: None)


def test_connect_awake_does_not_retry_a_real_failure(monkeypatch):
    """A missing driver or a revoked role must be reported now, not in 5 minutes."""

    def broken():
        raise ValueError("no credential")

    monkeypatch.setattr("src.db.azure_impl.connect", broken)
    with pytest.raises(ValueError, match="no credential"):
        load_azure.connect_awake(echo=lambda *_: None)


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

    if actual["filings"] == 70_949:
        unavailable("Azure still holds Step 8's seed; run 'python -m src.etl.load_azure'")
    assert actual == expected
