"""Load the H-1B filings into Azure SQL, from the Parquet caches in Blob.

Plan Step 9. This is the job that runs in the ETL container: it downloads the
nine DOL caches from ``raw/``, runs Phase 1's cleaning over them, writes the
cleaned frame to ``curated/`` as an auditable artifact, and bulk inserts the six
tables into Azure SQL.

**Nothing here decides what the data means.** The deduplication comes from
:func:`src.ingest.combine`, the cleaning from :func:`src.clean.clean`, and the
table shapes and every primary key from :func:`src.load.build_tables`. This
module is a transport: download, call those three, write. That is deliberate —
a second implementation of "which title_id means which title" would not raise
when it disagreed with Phase 1's, it would silently attach the wrong employers
to the right wages, and the Step 8 equality tests would go on passing because
both sides would be reading the same wrong ids.

Three things here are load-bearing:

**The Parquet caches are the input, not the spreadsheets.** ``ingest.load_all``
reads ``.xlsx`` and builds the caches; the container has no ``.xlsx`` and no
openpyxl budget to read them with. It reads the caches directly and hands them
to :func:`src.ingest.combine`, which is the half of ``load_all`` that resolves
the 20,873 cases appearing in two files. Skipping it loads 871,194 rows.

**The load replaces, it does not append.** See :func:`clear`.

**``fast_executemany`` is not optional.** See :func:`insert`.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from src import clean, ingest, load
from src.db.azure_impl import connect_awake
from src.etl import blob

__all__ = [
    "download_caches",
    "read_cleaned",
    "clear",
    "connect_awake",
    "insert",
    "run",
    "main",
]

#: The ETL job waits longer for a resume than a person on a spinner would.
#:
#: The retry itself lives on the backend — see
#: :func:`src.db.azure_impl.connect_awake`. It was here first, which meant the
#: loader survived a paused database and the dashboard did not.
RESUME_TIMEOUT = 300.0
RESUME_WAIT = 15.0

#: Rows per ``executemany`` round trip.
#:
#: The plan says 10,000 and that is what this is. The number is a trade between
#: round trips and the parameter limit: Azure SQL caps a single statement at
#: 2,100 parameters, which ``fast_executemany`` sidesteps by binding a column
#: array rather than expanding the rows — so the cap does not apply, and the
#: batch size is purely about memory and retry granularity.
BATCH = 10_000

#: The table the whole run is judged by.
FILINGS = "filings"


def download_caches(dest: Path) -> list[Path]:
    """Pull every DOL cache out of ``raw/`` into ``dest``. Returns local paths.

    Selection is by the same ``LCA_*.parquet`` rule the upload used, so a blob
    that is not a DOL cache — a probe file, a manual experiment — is ignored
    here exactly as it was ignored there, rather than being read as data.
    """
    dest.mkdir(parents=True, exist_ok=True)
    names = [name for name, _ in blob.list_raw()]
    wanted = [n for n in names if n.startswith("LCA_") and n.endswith(".parquet")]
    if not wanted:
        raise FileNotFoundError(
            f"{blob.account_name()}/{blob.RAW_CONTAINER} holds no LCA_*.parquet "
            "caches. If it has been ~90 days since the last upload, the "
            "lifecycle rule has emptied it; re-run 'python -m src.etl.blob "
            f"upload'. Found instead: {names}"
        )
    return [blob.download_raw(name, dest) for name in sorted(wanted)]


def read_cleaned(paths: list[Path]) -> pd.DataFrame:
    """Read the caches, resolve duplicate cases, and clean. One row per filing.

    Reads only the columns ``clean`` declares it needs, plus the two
    :func:`src.ingest.combine` needs to deduplicate — the same narrowing
    ``load_all`` does, via the same helper, so the container cannot read a
    different set of columns than the laptop does.
    """
    columns = ingest.needed_columns(clean.SOURCE_COLUMNS)
    frames = [pd.read_parquet(path, columns=columns) for path in paths]
    return clean.clean(ingest.combine(frames))


def write_curated(cleaned: pd.DataFrame, name: str = "filings_clean.parquet") -> str:
    """Publish the cleaned frame to ``curated/``. Returns the blob name.

    The plan asks for this and it earns its place independently: it is the only
    artifact that shows what was actually loaded, as opposed to what the source
    files happened to contain on the day. When a count in the runbook disagrees
    with the database, this is the thing to diff.
    """
    with tempfile.TemporaryDirectory() as directory:
        local = Path(directory) / name
        cleaned.to_parquet(local, compression="snappy", index=False)
        return blob.upload_raw(local, container=blob.CURATED_CONTAINER)


def clear(cursor) -> None:
    """Empty every table, children before parents.

    **The load replaces rather than appends, and it has to.** Every primary key
    is assigned by ``range(len(values))`` in :func:`src.load.build_tables`, so a
    second run recomputes ids from 0 and an append collides on the first row.
    The database is also not empty to begin with: Step 8 seeded the five lookups
    in full plus 70,949 filings so the equality tests had something to compare.

    ``filings`` is TRUNCATE-able because nothing references it — the foreign
    keys point *out* of it. The lookups are not, even once ``filings`` is empty:
    SQL Server refuses TRUNCATE on any table named by a FOREIGN KEY constraint
    regardless of whether the referencing table holds rows. Hence DELETE there,
    which is fine: the five lookups are ~176,000 rows between them against
    ``filings``' 850,321.
    """
    cursor.execute(f"TRUNCATE TABLE dbo.{FILINGS}")
    for table, _, _, _ in reversed(load.LOOKUPS):
        cursor.execute(f"DELETE FROM dbo.{table}")


def _batches(frame: pd.DataFrame, size: int = BATCH):
    """Yield ``size``-row lists of tuples, NULLs as ``None``.

    Converted per batch rather than all at once. ``filings`` is 850,321 rows
    wide enough that materializing every tuple first costs over a gigabyte in a
    container that does not have one to spare.

    ``pd.NA`` and ``NaN`` both have to become ``None``: pyodbc binds ``NaN`` as
    the float it is, and a ``NaN`` arriving in an ``INT`` column fails the batch
    with a conversion error naming neither the row nor the column.
    """
    for start in range(0, len(frame), size):
        chunk = frame.iloc[start : start + size]
        values = chunk.astype(object).where(pd.notna(chunk), None)
        yield list(values.itertuples(index=False, name=None))


def insert(cursor, table: str, frame: pd.DataFrame, size: int = BATCH) -> int:
    """Bulk insert ``frame`` into ``table``. Returns rows written.

    ``fast_executemany`` is the difference between minutes and hours. Without
    it pyodbc issues one round trip per row: 850,321 round trips to a database
    in another country, which on a serverless free tier burns the monthly vCore
    grant before it finishes. With it, each batch is one round trip binding
    column arrays.

    Column order comes from the frame, and the INSERT names its columns
    explicitly rather than relying on the table's declared order — the schema
    and ``build_tables`` agree today, and a positional INSERT would turn any
    future disagreement into silently transposed data instead of an error.
    """
    columns = list(frame.columns)
    placeholders = ", ".join("?" for _ in columns)
    statement = (
        f"INSERT INTO dbo.{table} ({', '.join(columns)}) VALUES ({placeholders})"
    )
    cursor.fast_executemany = True
    written = 0
    for rows in _batches(frame, size):
        cursor.executemany(statement, rows)
        written += len(rows)
    return written


def run(
    *,
    cache_dir: Path | None = None,
    publish_curated: bool = True,
    echo=print,
) -> dict[str, int]:
    """Download, clean, publish, load. Returns rows written per table.

    The whole write is one transaction. A failure partway through a six-table
    load would otherwise leave the database with lookups from this run and
    filings from the last, which is not a state any count would reveal — the row
    totals would look plausible and the joins would be wrong.
    """
    started = time.time()

    # Prove the database is reachable before doing two minutes of work that only
    # matters if it is.
    #
    # This is not tidiness. The third container run downloaded 9 caches, cleaned
    # 850,321 filings and published the curated parquet — 115 seconds — and then
    # failed to open the ODBC driver, which is a condition that was already true
    # when the process started. Everything before the failure was wasted, and
    # because the container log stream retains almost nothing and dies with the
    # replica, the useful line had to be recovered from a log that had nearly
    # rotated it away.
    #
    # It also removes a confound. A connection that fails only *after* the tables
    # are built cannot be told apart from one that fails because they were built;
    # connecting first and last answers that question with the run itself rather
    # than with an argument.
    connect_awake(timeout=RESUME_TIMEOUT, wait=RESUME_WAIT, echo=echo).close()
    echo("database reachable")

    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(cache_dir) if cache_dir else Path(scratch)
        paths = download_caches(directory)
        echo(f"downloaded {len(paths)} caches")

        cleaned = read_cleaned(paths)
        echo(f"cleaned {len(cleaned):,} filings")

        if publish_curated:
            echo(f"published {write_curated(cleaned)}")

        tables = load.build_tables(cleaned)

        # `tables` holds everything the load needs from here on, and `cleaned` is
        # ~0.5 GB that would otherwise stay alive through the whole insert for
        # the sake of one integer in _verify. Measured: 2.52 GB live at this
        # point, 2.01 GB after.
        expected = len(cleaned)
        del cleaned

    connection = connect_awake(timeout=RESUME_TIMEOUT, wait=RESUME_WAIT, echo=echo)
    written: dict[str, int] = {}
    try:
        cursor = connection.cursor()
        clear(cursor)
        for table, frame in tables.items():
            at = time.time()
            written[table] = insert(cursor, table, frame)
            echo(f"{table:<14}{written[table]:>10,} rows  {time.time() - at:6.1f}s")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    _verify(written, expected)
    echo(f"loaded in {time.time() - started:.0f}s")
    return written


def _verify(written: dict[str, int], expected: int) -> None:
    """Refuse to call a load successful without checking what it wrote.

    Counts what the cursor reported rather than re-querying: a second SELECT
    would resume a paused database and cost a round trip to re-learn something
    already known. The database's own count is checked separately by
    ``tests/test_load_azure.py``, which is where a discrepancy between "rows
    sent" and "rows stored" would show up.
    """
    if written.get(FILINGS) != expected:
        raise RuntimeError(
            f"wrote {written.get(FILINGS):,} filings but cleaned {expected:,}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=os.environ.get("H1B_CACHE_DIR") or None,
        help="keep the downloaded caches here instead of a temporary directory",
    )
    parser.add_argument(
        "--no-curated",
        action="store_true",
        help="skip publishing the cleaned frame to the curated container",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="connect, report, and exit without touching the data",
    )
    args = parser.parse_args(argv)

    # A way to ask "can this container reach the database" that does not erase
    # it first.
    #
    # Both images once shipped and deployed with an ODBC driver that could not
    # load, and nothing caught it: the job failed after two minutes of work, and
    # the dashboard served HTTP 200 with the error inside the page, where the
    # deployment's own health check cannot see it. The only way to exercise the
    # driver was a full destructive load, which is far too expensive to use as a
    # smoke test. This is that test, and it is the same code path the load uses.
    if args.check:
        try:
            connect_awake(timeout=RESUME_TIMEOUT, wait=RESUME_WAIT, echo=print).close()
        except Exception as exc:  # noqa: BLE001 - the exit code is the signal
            print(f"check failed: {exc}", file=sys.stderr)
            return 1
        print("database reachable")
        return 0

    try:
        run(cache_dir=args.cache_dir, publish_curated=not args.no_curated)
    except Exception as exc:  # noqa: BLE001 - the container's exit code is the signal
        print(f"load failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
