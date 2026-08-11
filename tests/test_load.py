"""Tests for :mod:`src.load`.

All of these build a real SQLite file in a temporary directory from a handful
of handwritten filings, so the suite stays fast and needs no source data. The
figures from the full 850,321-row build are checked in
``test_pipeline_numbers.py`` instead.

The schema here is not the one in the plan's §6. That version stores
``job_title`` inline and wages as REAL, which measured 148 MB — above the
100 MB file that GitHub will refuse to accept. The tests that pin the size
decisions are marked as such, because those choices are the whole reason the
schema looks the way it does.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from src import clean, load

DEFAULTS: dict[str, object] = {
    "CASE_NUMBER": "I-200-25001-000001",
    "CASE_STATUS": "Certified",
    "VISA_CLASS": "H-1B",
    "DECISION_DATE": "2025-01-15",
    "RECEIVED_DATE": "2024-11-01",
    "EMPLOYER_NAME": "ACME INC",
    "JOB_TITLE": "Software Engineer",
    "SOC_CODE": "15-1252.00",
    "SOC_TITLE": "Software Developers",
    "WORKSITE_CITY": "austin",
    "WORKSITE_STATE": "tx",
    "WAGE_RATE_OF_PAY_FROM": 100_000.0,
    "WAGE_RATE_OF_PAY_TO": None,
    "WAGE_UNIT_OF_PAY": "Year",
    "PREVAILING_WAGE": 95_000.0,
    "PW_UNIT_OF_PAY": "Year",
    "FULL_TIME_POSITION": "Y",
}


def cleaned(**overrides: object) -> pd.DataFrame:
    """Cleaned filings built from the defaults, one row per list entry."""
    rows = max(
        (len(v) for v in overrides.values() if isinstance(v, list)), default=1
    )
    fields = {**DEFAULTS, **overrides}
    raw = pd.DataFrame(
        {k: v if isinstance(v, list) else [v] * rows for k, v in fields.items()}
    )
    return clean.clean(raw)


@pytest.fixture
def db(tmp_path):
    """A built database and an open connection to it."""
    frame = cleaned(
        CASE_NUMBER=["I-200-25001-000001", "I-203-25002-000002"],
        EMPLOYER_NAME=["ACME INC", "Acme, Inc."],
        JOB_TITLE=["Software Engineer", "Data Scientist"],
        SOC_CODE=["15-1252.00", "15-2051.00"],
        SOC_TITLE=["Software Developers", "Data Scientists"],
        WORKSITE_CITY=["austin", "portland"],
        WORKSITE_STATE=["tx", "or"],
        VISA_CLASS=["H-1B", "E-3 Australian"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 60.0],
        # A band with an odd span gives a midpoint ending in .5, which is the
        # only case where rounding before storage changes anything: SQLite's
        # INTEGER affinity silently converts a whole float, but keeps a
        # fractional one as REAL.
        WAGE_RATE_OF_PAY_TO=[140_001.0, None],
        WAGE_UNIT_OF_PAY=["Year", "Hour"],
    )
    path, _ = load.build(frame, tmp_path / "h1b.db")
    connection = load.connect(path)
    yield connection, path, frame
    connection.close()


# --------------------------------------------------------------------------
# The plan's "done when"
# --------------------------------------------------------------------------


def test_every_cleaned_row_reaches_the_database(db):
    connection, _, frame = db
    assert connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == len(
        frame
    )


def test_building_twice_produces_an_identical_file(tmp_path):
    """Idempotent by replacement, so a crashed load leaves nothing behind."""
    frame = cleaned(CASE_NUMBER=["I-200-25001-000001", "I-200-25001-000002"])
    first = (tmp_path / "a.db", tmp_path / "b.db")
    load.build(frame, first[0])
    load.build(frame, first[0])
    load.build(frame, first[1])
    assert first[0].read_bytes() == first[1].read_bytes()


def test_a_failed_build_leaves_the_previous_database_untouched(tmp_path, monkeypatch):
    """Writing in place replaces a good file with a valid-looking empty one.

    That is the worst shape a failure can take here: a dashboard reading it
    renders "no results" rather than an error, so nobody finds out.
    """
    path = tmp_path / "h1b.db"
    load.build(cleaned(CASE_NUMBER=["I-200-25001-00000%d" % i for i in (1, 2)]), path)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("simulated failure partway through the load")

    monkeypatch.setattr(load, "split_case_number", fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        load.build(cleaned(CASE_NUMBER=["I-200-25001-000003"]), path)

    assert path.read_bytes() == before
    connection = load.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 2
    connection.close()


def test_a_failed_build_leaves_no_scratch_files_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(load, "split_case_number", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        load.build(cleaned(), tmp_path / "h1b.db")
    assert list(tmp_path.iterdir()) == []


def test_concurrent_builds_do_not_corrupt_the_database(tmp_path):
    """Each build writes to its own scratch name, so the last rename wins."""
    import threading

    path = tmp_path / "h1b.db"
    frame = cleaned(CASE_NUMBER=["I-200-25001-00000%d" % i for i in (1, 2, 3)])
    errors: list[Exception] = []

    def go():
        try:
            load.build(frame, path)
        except Exception as exc:  # noqa: BLE001 - the point is to see any of them
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    connection = load.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 3
    connection.close()


def test_the_schema_is_internally_consistent(db):
    connection, _, _ = db
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --------------------------------------------------------------------------
# The view is the readable face of a schema built for size
# --------------------------------------------------------------------------


def test_the_view_reassembles_the_case_number(db):
    connection, _, frame = db
    got = [r[0] for r in connection.execute("SELECT case_number FROM v_filings")]
    assert sorted(got) == sorted(frame["case_number"])


def test_the_view_round_trips_a_whole_filing(db):
    connection, _, frame = db
    row = connection.execute(
        "SELECT employer_name, job_title, worksite_city, worksite_state, "
        "annual_wage, visa_class, case_status FROM v_filings WHERE case_number = ?",
        ("I-200-25001-000001",),
    ).fetchone()
    source = frame[frame["case_number"].eq("I-200-25001-000001")].iloc[0]
    assert row == (
        source["employer_name"],
        source["job_title"],
        source["worksite_city"],
        source["worksite_state"],
        round(source["annual_wage"]),
        source["visa_class"],
        source["case_status"],
    )


def test_withdrawn_filings_read_back_as_their_original_status(tmp_path):
    frame = cleaned(
        CASE_NUMBER=["I-200-25001-000001", "I-200-25001-000002"],
        CASE_STATUS=["Certified", "Certified - Withdrawn"],
    )
    path, _ = load.build(frame, tmp_path / "h1b.db")
    connection = load.connect(path)
    statuses = {r[0] for r in connection.execute("SELECT case_status FROM v_filings")}
    connection.close()
    assert statuses == {"Certified", "Certified - Withdrawn"}


# --------------------------------------------------------------------------
# Case numbers, which are the primary key
# --------------------------------------------------------------------------


def test_case_numbers_split_into_prefix_and_serial():
    prefix, serial = load.split_case_number(pd.Series(["I-203-25001-000042"]))
    assert prefix.tolist() == [203]
    assert serial.tolist() == [25001000042]


@pytest.mark.parametrize(
    "bad", ["I-200-25001", "200-25001-000001", "I-200-2500A-000001", ""]
)
def test_an_unexpected_case_number_format_raises(bad):
    """DOL changing the format must not become a confusing key error later."""
    with pytest.raises(ValueError, match="I-nnn-nnnnn-nnnnnn"):
        load.split_case_number(pd.Series([bad]))


def test_a_repeated_serial_raises_before_it_reaches_the_primary_key():
    with pytest.raises(ValueError, match="unique"):
        load.split_case_number(pd.Series(["I-200-25001-000001"] * 2))


# --------------------------------------------------------------------------
# Lookup tables — the reason the file fits
# --------------------------------------------------------------------------


def test_a_repeated_title_is_stored_once(tmp_path):
    """123,990 distinct titles across 850,321 filings; inlining them costs 29 MB."""
    frame = cleaned(
        CASE_NUMBER=["I-200-25001-00000%d" % i for i in (1, 2, 3)],
        JOB_TITLE=["Software Engineer", "Software Engineer", "Data Scientist"],
    )
    path, _ = load.build(frame, tmp_path / "h1b.db")
    connection = load.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM titles").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 3
    connection.close()


def test_the_same_city_name_in_two_states_is_two_locations(tmp_path):
    """Keyed on the pair. There is a Portland in both OR and ME."""
    frame = cleaned(
        CASE_NUMBER=["I-200-25001-000001", "I-200-25001-000002"],
        WORKSITE_CITY=["portland", "portland"],
        WORKSITE_STATE=["or", "me"],
    )
    path, _ = load.build(frame, tmp_path / "h1b.db")
    connection = load.connect(path)
    rows = connection.execute(
        "SELECT worksite_city, worksite_state FROM locations ORDER BY worksite_state"
    ).fetchall()
    connection.close()
    assert rows == [("Portland", "ME"), ("Portland", "OR")]


def test_all_visa_classes_are_loaded(tmp_path):
    """E-3 and H-1B1 are the same form under the same wage rules; see README."""
    frame = cleaned(
        CASE_NUMBER=["I-200-25001-00000%d" % i for i in (1, 2, 3)],
        VISA_CLASS=["H-1B", "E-3 Australian", "H-1B1 Singapore"],
    )
    path, _ = load.build(frame, tmp_path / "h1b.db")
    connection = load.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 3
    assert connection.execute("SELECT COUNT(*) FROM visa_classes").fetchone()[0] == 3
    connection.close()


# --------------------------------------------------------------------------
# Behaviour that would fail silently
# --------------------------------------------------------------------------


def test_foreign_keys_are_actually_enforced(db):
    """SQLite ignores REFERENCES unless asked, per connection, every time."""
    connection, _, _ = db
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO filings (case_serial, case_prefix, employer_id, soc_id, "
            "title_id, visa_class_id, fiscal_year, full_time, withdrawn) "
            "VALUES (99999999999, 200, 424242, 1, 1, 1, 2025, 1, 0)"
        )


def test_a_missing_city_keeps_the_filing(tmp_path):
    """A NULL city must not drop the row out of every non-city aggregate."""
    frame = cleaned(WORKSITE_CITY=None)
    path, _ = load.build(frame, tmp_path / "h1b.db")
    connection = load.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM v_filings").fetchone()[0] == 1
    connection.close()


def test_wages_are_stored_as_whole_dollar_integers(db):
    """REAL costs 8 bytes per value and no wage here needs cents.

    Checked with ``typeof`` on the stored values, not ``PRAGMA table_info``.
    SQLite column types are advisory — it will happily keep a float in a
    column declared INTEGER, so reading the declaration proves nothing.
    """
    connection, _, _ = db
    for column in ("annual_wage", "annual_from", "annual_to", "prevailing_wage"):
        stored = {
            row[0]
            for row in connection.execute(
                f"SELECT DISTINCT typeof({column}) FROM filings"
            )
        }
        assert stored <= {"integer", "null"}, f"{column} stored as {stored}"


def test_rebuilding_over_an_existing_file_replaces_it(tmp_path):
    path = tmp_path / "h1b.db"
    load.build(cleaned(CASE_NUMBER=["I-200-25001-00000%d" % i for i in (1, 2)]), path)
    load.build(cleaned(CASE_NUMBER=["I-200-25001-000003"]), path)
    connection = load.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 1
    connection.close()
