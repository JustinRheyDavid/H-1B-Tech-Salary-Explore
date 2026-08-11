"""Build ``data/h1b.db`` from the cleaned filings.

The database is committed to the repository so the deployed dashboard needs no
credentials and no source data. That decision sets the constraint everything
here is shaped by: **GitHub hard-rejects any file over 100 MB.** The plan's
three-table schema with ``job_title`` stored inline produces 148 MB, which
cannot be pushed at all.

So the text columns become lookup tables and the numbers become integers:

===========================================  ========
schema                                          size
===========================================  ========
plan's §6 three tables, wages as REAL           148 MB
+ job_title lookup table                        119 MB
+ city/location and integer wages and flags      96 MB
+ case number split into prefix and serial       87 MB
===========================================  ========

Every one of those was measured, not estimated. Rows are never dropped to hit
the number — all 850,321 cleaned filings are loaded.

Running this twice produces the same file: the tables are dropped and rebuilt
rather than appended to, so there is no partial-load state to reason about.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from src import clean, ingest

__all__ = ["SCHEMA", "build", "connect", "summarize"]

DB_PATH = Path("data/h1b.db")
RAW_DIR = Path("data/raw")
INTERIM_DIR = Path("data/interim")

# One row per distinct key, referenced by integer id from ``filings``.
# (table, id column, key columns, payload carried alongside the key)
# locations is keyed on the pair: there is a Portland in both OR and ME.
LOOKUPS: list[tuple[str, str, list[str], str | None]] = [
    ("employers", "employer_id", ["employer_name"], "raw_name_sample"),
    ("occupations", "soc_id", ["soc_code"], "soc_title"),
    ("titles", "title_id", ["job_title"], None),
    ("locations", "location_id", ["worksite_city", "worksite_state"], None),
    ("visa_classes", "visa_class_id", ["visa_class"], None),
]

SCHEMA = """
CREATE TABLE employers (
    employer_id     INTEGER PRIMARY KEY,
    employer_name   TEXT NOT NULL UNIQUE,   -- normalized form
    raw_name_sample TEXT                    -- one original spelling, for auditability
);

CREATE TABLE occupations (
    soc_id    INTEGER PRIMARY KEY,
    soc_code  TEXT NOT NULL UNIQUE,         -- e.g. '15-2051'
    soc_title TEXT NOT NULL                 -- e.g. 'Data Scientists'
);

-- Employer job titles, kept exactly as filed. Normalizing them would destroy
-- the signal users search on; storing them once instead of 850,321 times is a
-- storage decision, not a data one, and saves 29 MB.
CREATE TABLE titles (
    title_id  INTEGER PRIMARY KEY,
    job_title TEXT NOT NULL UNIQUE
);

CREATE TABLE locations (
    location_id    INTEGER PRIMARY KEY,
    worksite_city  TEXT NOT NULL,
    worksite_state TEXT NOT NULL,           -- 2-letter
    UNIQUE (worksite_city, worksite_state)
);

-- H-1B, plus E-3 (Australia) and H-1B1 (Chile/Singapore), which are filed on
-- the same form under the same wage rules. All are loaded; see the README.
CREATE TABLE visa_classes (
    visa_class_id INTEGER PRIMARY KEY,
    visa_class    TEXT NOT NULL UNIQUE
);

CREATE TABLE filings (
    -- Case numbers look like 'I-200-25001-000001': a prefix that is one of
    -- four values, then an 11-digit integer that is unique across all 850,321
    -- filings. Storing the serial as INTEGER PRIMARY KEY makes it the rowid,
    -- so uniqueness is enforced with no index of its own — a separate UNIQUE
    -- column costs 12.8 MB for the same guarantee. v_filings reassembles it.
    case_serial     INTEGER PRIMARY KEY,
    case_prefix     INTEGER NOT NULL,        -- the 200 in 'I-200-'

    employer_id     INTEGER NOT NULL REFERENCES employers(employer_id),
    soc_id          INTEGER NOT NULL REFERENCES occupations(soc_id),
    title_id        INTEGER NOT NULL REFERENCES titles(title_id),
    location_id     INTEGER REFERENCES locations(location_id),
    visa_class_id   INTEGER NOT NULL REFERENCES visa_classes(visa_class_id),

    -- Whole dollars per year. SQLite stores a small integer in 1-6 bytes and a
    -- REAL in 8, and no wage here needs cents.
    annual_wage     INTEGER,                -- midpoint when the filing gave a band
    annual_from     INTEGER,                -- the band's low end as filed
    annual_to       INTEGER,                -- the band's high end, NULL if no band
    prevailing_wage INTEGER,

    fiscal_year     INTEGER NOT NULL,
    full_time       INTEGER NOT NULL,       -- 0/1
    withdrawn       INTEGER NOT NULL,       -- 1 = 'Certified - Withdrawn'

    -- Repairs and exclusions, kept so every decision stays auditable.
    is_outlier      INTEGER NOT NULL DEFAULT 0,
    pw_outlier      INTEGER NOT NULL DEFAULT 0,
    unit_repaired   INTEGER NOT NULL DEFAULT 0,
    pw_repaired     INTEGER NOT NULL DEFAULT 0
);

-- Indexes cost 9 MB each on this many rows, so only the columns queries
-- actually filter on get one. fiscal_year (3 distinct values) and soc_id (63)
-- are too low-cardinality for an index to beat a scan, and employer_id is
-- grouped rather than filtered. titles.job_title needs no index of its own:
-- its UNIQUE constraint already built one, which is what title_search uses.
CREATE INDEX idx_filings_title    ON filings(title_id);
CREATE INDEX idx_filings_location ON filings(location_id);

-- Everything joined and the case number reassembled, so a person auditing a
-- row does not have to remember the id columns or the prefix split.
CREATE VIEW v_filings AS
SELECT printf('I-%03d-%05d-%06d', f.case_prefix,
              f.case_serial / 1000000, f.case_serial % 1000000) AS case_number,
       e.employer_name,
       e.raw_name_sample AS employer_as_filed,
       t.job_title,
       o.soc_code,
       o.soc_title,
       l.worksite_city,
       l.worksite_state,
       v.visa_class,
       f.annual_wage, f.annual_from, f.annual_to, f.prevailing_wage,
       f.fiscal_year,
       f.full_time,
       CASE f.withdrawn WHEN 1 THEN 'Certified - Withdrawn' ELSE 'Certified' END
           AS case_status,
       f.is_outlier, f.pw_outlier, f.unit_repaired, f.pw_repaired
FROM filings f
JOIN employers    e ON e.employer_id    = f.employer_id
JOIN occupations  o ON o.soc_id         = f.soc_id
JOIN titles       t ON t.title_id       = f.title_id
JOIN visa_classes v ON v.visa_class_id  = f.visa_class_id
LEFT JOIN locations l ON l.location_id  = f.location_id;
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open ``path`` with foreign keys enforced.

    SQLite ignores ``REFERENCES`` unless asked, per connection, every time.
    A schema whose constraints are decorative is worse than one without them.
    """
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def split_case_number(numbers: pd.Series) -> tuple[pd.Series, pd.Series]:
    """``I-200-25001-000001`` into ``(200, 25001000001)``.

    Raises rather than coercing: a case number outside this shape means DOL
    changed the format, and a silent NULL would surface much later as a
    confusing primary-key error with nothing pointing back to the cause.
    """
    parts = numbers.str.extract(r"^I-(\d{3})-(\d{5})-(\d{6})$")
    if parts.isna().any(axis=None):
        bad = numbers[parts[0].isna()]
        raise ValueError(
            f"{len(bad)} case numbers are not in the expected "
            f"I-nnn-nnnnn-nnnnnn form, e.g. {bad.head(3).tolist()}"
        )
    prefix = parts[0].astype("int64")
    serial = (parts[1] + parts[2]).astype("int64")

    if serial.duplicated().any():
        raise ValueError(
            f"{int(serial.duplicated().sum())} case numbers share a serial; "
            "it is the primary key and must be unique"
        )
    return prefix, serial


def _write_lookup(
    connection: sqlite3.Connection,
    frame: pd.DataFrame,
    table: str,
    id_column: str,
    keys: list[str],
    payload: str | None,
) -> pd.Series:
    """Write one lookup table; return the id each row of ``frame`` maps to.

    Joined back rather than mapped through a dict so that a multi-column key
    works the same way a single-column one does.
    """
    columns = keys + ([payload] if payload else [])
    values = frame[columns].drop_duplicates(subset=keys).reset_index(drop=True)
    values.insert(0, id_column, range(len(values)))
    values.to_sql(table, connection, if_exists="append", index=False)

    ids = frame[keys].merge(values[[*keys, id_column]], on=keys, how="left")
    if ids[id_column].isna().any():
        raise RuntimeError(f"{table}: {int(ids[id_column].isna().sum())} rows unmatched")
    return pd.Series(ids[id_column].to_numpy(), index=frame.index).astype("int64")


def build(
    frame: pd.DataFrame | None = None, path: Path = DB_PATH
) -> tuple[Path, pd.Series]:
    """Create ``path`` from cleaned filings. Returns the path and stage counts.

    Idempotent by deletion: the file is replaced, not updated. Re-running after
    a crash cannot leave half a load behind, which is the only failure mode a
    dashboard reading this file would have no way to detect.
    """
    if frame is None:
        raw = ingest.load_all(RAW_DIR, INTERIM_DIR, clean.SOURCE_COLUMNS)
        cleaned = clean.clean(raw)
        counts = clean.stage_counts(raw, cleaned=cleaned)
    else:
        cleaned = frame
        counts = pd.Series({"rows out": len(cleaned)}, dtype="int64")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    connection = connect(path)
    try:
        connection.executescript(SCHEMA)

        # NOT NULL on the lookup keys means a missing city cannot be NULL here;
        # it becomes its own row and stays visibly empty rather than silently
        # dropping the filing out of every city aggregate.
        source = cleaned.assign(
            raw_name_sample=cleaned["employer_raw"],
            worksite_city=cleaned["worksite_city"].fillna(""),
            worksite_state=cleaned["worksite_state"].fillna(""),
        )

        filings = pd.DataFrame(index=cleaned.index)
        for table, id_column, keys, payload in LOOKUPS:
            filings[id_column] = _write_lookup(
                connection, source, table, id_column, keys, payload
            )

        prefix, serial = split_case_number(cleaned["case_number"])
        filings["case_prefix"] = prefix
        filings["case_serial"] = serial

        for column in ("annual_wage", "annual_from", "annual_to", "prevailing_wage"):
            filings[column] = cleaned[column].round().astype("Int64")

        filings["fiscal_year"] = cleaned["fiscal_year"].astype("int64")
        filings["full_time"] = cleaned["full_time"].astype("int64")
        filings["withdrawn"] = (
            cleaned["case_status"].eq("Certified - Withdrawn").astype("int64")
        )
        for flag in ("is_outlier", "pw_outlier", "unit_repaired", "pw_repaired"):
            filings[flag] = cleaned[flag].astype("int64")

        filings.to_sql("filings", connection, if_exists="append", index=False)
        connection.commit()

        loaded = connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        if loaded != len(cleaned):
            raise RuntimeError(
                f"loaded {loaded:,} rows but cleaned {len(cleaned):,}"
            )
    finally:
        connection.close()

    # VACUUM cannot run inside the transaction above, and needs its own
    # connection because the schema script leaves one open implicitly.
    with sqlite3.connect(path) as vacuum:
        vacuum.execute("VACUUM")
    vacuum.close()

    return path, counts


def summarize(path: Path = DB_PATH) -> str:
    """One line per table, plus the file size the 100 MB limit applies to."""
    connection = connect(path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        lines = [
            f"  {table:<14} {connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>9,}"
            for table in tables
        ]
    finally:
        connection.close()
    size = Path(path).stat().st_size / 1e6
    lines.append(f"  {'file size':<14} {size:>8.1f} MB   (GitHub rejects over 100 MB)")
    return "\n".join(lines)


def main() -> None:
    path, counts = build()
    print("Rows discarded between the source files and the database:")
    print(counts.to_string().replace("\n", "\n  ").rjust(2))
    print(f"\nBuilt {path}:")
    print(summarize(path))


if __name__ == "__main__":
    main()
