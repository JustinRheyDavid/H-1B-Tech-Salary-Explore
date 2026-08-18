-- The Azure SQL schema. A port of src/load.py's SCHEMA, table for table.
--
-- Plan Step 8. Run as the Entra admin OR as h1b-etl — the ETL identity holds
-- CREATE TABLE, CREATE VIEW, ALTER and REFERENCES on dbo from Step 6, which is
-- what lets Step 9's job build this itself:
--
--   sqlcmd -b -S sql-h1b-hutymqa65yoty.database.windows.net -d sqldb-h1b \
--     --authentication-method ActiveDirectoryAzCli -i sql/schema_azure.sql
--
-- Creates only what is missing, so it is safe to re-run and safe to run against
-- a loaded database. It deliberately does NOT drop anything: a DDL script that
-- resets the database on every run is one stray execution away from deleting
-- 850,321 loaded rows. To rebuild from nothing, drop the objects by hand first.
--
-- Three things here are load-bearing and each has cost somebody an evening
-- somewhere. In order of how quietly they fail:
--
--   1. COLLATE Latin1_General_BIN2 on the unique text columns. Without it the
--      titles load dies. See below.
--   2. Plain INT/BIGINT primary keys, NOT IDENTITY. Every key is assigned by
--      the loader.
--   3. BIGINT on case_serial. INT overflows partway through the load.

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

-- ---------------------------------------------------------------------------
-- Collation pre-flight. Plan §6 asks for this to be settled before Step 9
-- depends on it, and it had not been.
-- ---------------------------------------------------------------------------
--
-- Plan §6 predicted BIN2 alone would settle this. **It does not**, and the
-- measurement below is the reason `titles` looks the way it does.
--
-- Run 2026-08-17 against this database, one distinction at a time:
--
--     case            kept       trailing tab    kept
--     leading space   kept       trailing space  MERGED
--
-- BIN2 compares byte by byte, but SQL Server pads both operands to equal length
-- *before* the comparison runs, and no collation exempts that. So 'Data
-- Engineer' and 'Data Engineer ' are equal under BIN2 even though DATALENGTH
-- reports 26 bytes against 28.
--
-- Measured against data/h1b.db: BIN2 cuts title collisions from 9,286 to 2,773,
-- but those 2,773 are still refused by a plain UNIQUE — and **44,045 filings,
-- 5.2% of the data, point at them**. The worst groups are the most common
-- titles in the dataset: 'Software Engineer' has four spellings differing only
-- in trailing spaces, 'Data Engineer' likewise.
--
-- The fix is `titles.key_exact` below. This check now proves BIN2 keeps the
-- distinctions it *can* keep, so a future collation change is still caught.
CREATE TABLE #collation_check (t NVARCHAR(100) COLLATE Latin1_General_BIN2 UNIQUE);
INSERT #collation_check VALUES (N'Data Engineer');
INSERT #collation_check VALUES (N'DATA ENGINEER');                    -- case
INSERT #collation_check VALUES (CONCAT(N'Data Engineer', NCHAR(9)));  -- trailing tab
INSERT #collation_check VALUES (N' Data Engineer');                   -- leading space

DECLARE @distinct INT = (SELECT COUNT(*) FROM #collation_check);
IF @distinct <> 4
    THROW 50000, N'BIN2 no longer preserves case/tab/leading-space. Do not load.', 1;
DROP TABLE #collation_check;
PRINT 'collation check passed: BIN2 keeps case, tab and leading space distinct';
GO

-- ---------------------------------------------------------------------------
-- Lookups
-- ---------------------------------------------------------------------------
--
-- Every primary key below is a plain INT, assigned in Python by
-- load._write_lookup's range(len(values)). NOT IDENTITY, and this is not a
-- style choice: an IDENTITY column refuses an explicit insert without
-- SET IDENTITY_INSERT ON per table per session, and if you let it generate
-- instead it renumbers everything — detaching every foreign key in `filings`
-- from the row it pointed at, while the load *succeeds*. The dashboard would
-- then show the wrong employers against the right wages.
--
-- NVARCHAR widths are sized from the data (plan §6): longest title 60 chars,
-- employer 89, city 35, soc_title 56. The declared sizes leave headroom.

IF OBJECT_ID('dbo.employers', 'U') IS NULL
CREATE TABLE dbo.employers (
    employer_id     INT           NOT NULL PRIMARY KEY,
    -- BIN2 here is belt-and-braces: clean.py normalizes employer names, so no
    -- collisions exist today. It is declared anyway so that a future change to
    -- normalization cannot quietly start merging two employers into one row.
    employer_name   NVARCHAR(120) COLLATE Latin1_General_BIN2 NOT NULL UNIQUE,
    raw_name_sample NVARCHAR(200) NULL
);
GO

IF OBJECT_ID('dbo.occupations', 'U') IS NULL
CREATE TABLE dbo.occupations (
    soc_id    INT          NOT NULL PRIMARY KEY,
    soc_code  NVARCHAR(10) NOT NULL UNIQUE,   -- e.g. '15-2051'
    soc_title NVARCHAR(80) NOT NULL           -- e.g. 'Data Scientists'
);
GO

-- Employer job titles, kept exactly as filed. Normalizing them would destroy
-- the signal users search on.
--
-- THIS TABLE IS THE ONE THAT MATTERS, and it takes two mechanisms rather than
-- the one the plan expected.
--
-- BIN2 handles case: without it, 'Software Engineer' and 'SOFTWARE ENGINEER'
-- are one value and 6,599 titles are refused.
--
-- key_exact handles trailing spaces, which BIN2 does NOT — see the collation
-- check at the top of this file. Appending a sentinel makes a trailing space an
-- *interior* space, and interior spaces have never been padding, so
-- 'Data Engineer.' and 'Data Engineer .' compare unequal and both rows live.
-- Without it 2,773 titles are refused and 44,045 filings lose the row they
-- point at.
--
-- The UNIQUE therefore sits on key_exact, not on job_title. A true duplicate is
-- still refused — verified, not assumed — because identical inputs still
-- produce identical keys. PERSISTED so the index is on stored bytes rather than
-- recomputed per probe.
--
-- The sentinel is '.' only because it must be some character that is not
-- whitespace; nothing reads key_exact, and no query should.
IF OBJECT_ID('dbo.titles', 'U') IS NULL
CREATE TABLE dbo.titles (
    title_id  INT           NOT NULL PRIMARY KEY,
    job_title NVARCHAR(100) COLLATE Latin1_General_BIN2 NOT NULL,
    key_exact AS (job_title + N'.') PERSISTED,
    CONSTRAINT uq_titles_job_title UNIQUE (key_exact)
);
GO

-- Phase 1 got this index for free from the UNIQUE constraint on job_title.
-- Moving UNIQUE onto key_exact takes that away, so title_search — which runs on
-- every keystroke — needs it declared explicitly.
--
-- Note it cannot serve title_search as a seek regardless: the column is BIN2 and
-- the LIKE applies COLLATE Latin1_General_CI_AS, and a collation mismatch forces
-- a scan. It is here so the scan is over a narrow index rather than the table,
-- and because dropping the UNIQUE should not silently drop an index too.
-- Measure after Step 9 before assuming it earns its keep.
IF INDEXPROPERTY(OBJECT_ID('dbo.titles'), 'idx_titles_job_title', 'IndexID') IS NULL
CREATE INDEX idx_titles_job_title ON dbo.titles(job_title);
GO

IF OBJECT_ID('dbo.locations', 'U') IS NULL
CREATE TABLE dbo.locations (
    location_id    INT         NOT NULL PRIMARY KEY,
    worksite_city  NVARCHAR(60) COLLATE Latin1_General_BIN2 NOT NULL,
    worksite_state NVARCHAR(2)  COLLATE Latin1_General_BIN2 NOT NULL,
    CONSTRAINT uq_locations UNIQUE (worksite_city, worksite_state)
);
GO

-- H-1B, plus E-3 (Australia) and H-1B1 (Chile/Singapore), filed on the same
-- form under the same wage rules.
--
-- NVARCHAR(20), not (10). The stored values are the full descriptions, not the
-- short names: 'H-1B1 Singapore' is 15 characters and 'E-3 Australian' 14. At
-- (10) the load dies on error 8152 *after* the other four lookups have
-- committed, leaving a half-populated database.
IF OBJECT_ID('dbo.visa_classes', 'U') IS NULL
CREATE TABLE dbo.visa_classes (
    visa_class_id INT          NOT NULL PRIMARY KEY,
    visa_class    NVARCHAR(20) NOT NULL UNIQUE
);
GO

-- ---------------------------------------------------------------------------
-- Facts
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.filings', 'U') IS NULL
CREATE TABLE dbo.filings (
    -- BIGINT, not INT. Case numbers look like 'I-200-25001-000001' and the
    -- serial is the 11-digit tail; the largest in the data is 26,085,730,937,
    -- twelve times INT's ceiling of 2,147,483,647. INT here fails the load with
    -- an arithmetic overflow partway through, after the lookups have committed.
    case_serial     BIGINT   NOT NULL PRIMARY KEY,
    case_prefix     SMALLINT NOT NULL,        -- the 200 in 'I-200-'

    employer_id     INT      NOT NULL REFERENCES dbo.employers(employer_id),
    soc_id          INT      NOT NULL REFERENCES dbo.occupations(soc_id),
    title_id        INT      NOT NULL REFERENCES dbo.titles(title_id),
    location_id     INT      NULL     REFERENCES dbo.locations(location_id),
    visa_class_id   INT      NOT NULL REFERENCES dbo.visa_classes(visa_class_id),

    -- INT, whole dollars, matching Phase 1. NOT DECIMAL(12,2): Phase 1 rounds
    -- to whole dollars before it writes, so storing cents would invent a
    -- precision the data does not have and make the two backends compare
    -- unequal on rounding alone — which is exactly what Step 8 tests.
    annual_wage     INT      NULL,            -- midpoint when the filing gave a band
    annual_from     INT      NULL,            -- the band's low end as filed
    annual_to       INT      NULL,            -- the band's high end, NULL if no band
    prevailing_wage INT      NULL,

    fiscal_year     SMALLINT NOT NULL,
    full_time       BIT      NOT NULL,
    withdrawn       BIT      NOT NULL,        -- 1 = 'Certified - Withdrawn'

    -- Repairs and exclusions, kept so every decision stays auditable.
    is_outlier      BIT      NOT NULL CONSTRAINT df_filings_is_outlier    DEFAULT 0,
    pw_outlier      BIT      NOT NULL CONSTRAINT df_filings_pw_outlier    DEFAULT 0,
    unit_repaired   BIT      NOT NULL CONSTRAINT df_filings_unit_repaired DEFAULT 0,
    pw_repaired     BIT      NOT NULL CONSTRAINT df_filings_pw_repaired   DEFAULT 0
);
GO

-- The two columns the queries filter on, matching Phase 1's index choice.
--
-- The INCLUDE lists are a HYPOTHESIS, not a measured win. Phase 1 measured four
-- covering indexes on annual_wage at +58 MB for no improvement at all, because
-- SQLite sorts for a window function whether or not an index could supply the
-- order. Azure SQL may behave differently, and these queries join `locations`
-- regardless, so the table is touched either way. Measure after Step 9 and drop
-- the INCLUDE lists if they earn nothing — they are not free, and the free tier
-- allows 32 GB.
IF INDEXPROPERTY(OBJECT_ID('dbo.filings'), 'idx_filings_title', 'IndexID') IS NULL
CREATE INDEX idx_filings_title ON dbo.filings(title_id)
    INCLUDE (annual_wage, location_id, fiscal_year, is_outlier);
GO

IF INDEXPROPERTY(OBJECT_ID('dbo.filings'), 'idx_filings_location', 'IndexID') IS NULL
CREATE INDEX idx_filings_location ON dbo.filings(location_id)
    INCLUDE (annual_wage, title_id, fiscal_year, is_outlier);
GO

-- ---------------------------------------------------------------------------
-- v_filings — everything joined, the case number reassembled
-- ---------------------------------------------------------------------------
--
-- So a person auditing a row does not have to remember the id columns or the
-- prefix split. Same body as load.SCHEMA's view with two dialect changes:
--
--   printf('I-%03d-%05d-%06d', ...) has no T-SQL equivalent, so the zero
--   padding is done with RIGHT(REPLICATE(...)). FORMAT() would read better and
--   is roughly an order of magnitude slower, being a CLR call per row.
--
--   Integer division is already integer division in both dialects here, since
--   case_serial is BIGINT — no CAST needed, unlike wage_distribution's binning.
IF OBJECT_ID('dbo.v_filings', 'V') IS NOT NULL
    DROP VIEW dbo.v_filings;
GO

CREATE VIEW dbo.v_filings AS
SELECT 'I-'
       + RIGHT('000' + CAST(f.case_prefix AS varchar(3)), 3) + '-'
       + RIGHT('00000' + CAST(f.case_serial / 1000000 AS varchar(5)), 5) + '-'
       + RIGHT('000000' + CAST(f.case_serial % 1000000 AS varchar(6)), 6)
           AS case_number,
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
FROM dbo.filings f
JOIN dbo.employers    e ON e.employer_id   = f.employer_id
JOIN dbo.occupations  o ON o.soc_id        = f.soc_id
JOIN dbo.titles       t ON t.title_id      = f.title_id
JOIN dbo.visa_classes v ON v.visa_class_id = f.visa_class_id
LEFT JOIN dbo.locations l ON l.location_id = f.location_id;
GO

-- ---------------------------------------------------------------------------
-- Assertions. Same reasoning as sql/grant_identities.sql: a DDL script that
-- prints results for a human to read is indistinguishable from one that half
-- worked. With `sqlcmd -b` these make the exit code mean something.
-- ---------------------------------------------------------------------------
DECLARE @problems nvarchar(max) = N'';

IF OBJECT_ID('dbo.employers',    'U') IS NULL SET @problems += N'employers missing; ';
IF OBJECT_ID('dbo.occupations',  'U') IS NULL SET @problems += N'occupations missing; ';
IF OBJECT_ID('dbo.titles',       'U') IS NULL SET @problems += N'titles missing; ';
IF OBJECT_ID('dbo.locations',    'U') IS NULL SET @problems += N'locations missing; ';
IF OBJECT_ID('dbo.visa_classes', 'U') IS NULL SET @problems += N'visa_classes missing; ';
IF OBJECT_ID('dbo.filings',      'U') IS NULL SET @problems += N'filings missing; ';
IF OBJECT_ID('dbo.v_filings',    'V') IS NULL SET @problems += N'v_filings missing; ';

-- The collation is the whole reason the load survives; assert it landed rather
-- than trusting that the CREATE above ran.
IF NOT EXISTS (
    SELECT 1 FROM sys.columns c
    JOIN sys.objects o ON o.object_id = c.object_id
    WHERE o.name = 'titles' AND c.name = 'job_title'
      AND c.collation_name = 'Latin1_General_BIN2')
    SET @problems += N'titles.job_title is not BIN2 — the load will reject 6,599 titles by case; ';

-- And the other half. Without key_exact carrying the UNIQUE, trailing spaces
-- merge and 2,773 more titles are refused, taking 44,045 filings with them.
IF NOT EXISTS (
    SELECT 1 FROM sys.computed_columns c
    JOIN sys.objects o ON o.object_id = c.object_id
    WHERE o.name = 'titles' AND c.name = 'key_exact')
    SET @problems += N'titles.key_exact missing — trailing-space titles will be rejected; ';
IF EXISTS (
    SELECT 1 FROM sys.indexes i
    JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
    JOIN sys.objects o ON o.object_id = i.object_id
    WHERE o.name = 'titles' AND i.is_unique = 1 AND c.name = 'job_title')
    SET @problems += N'UNIQUE is on titles.job_title; it belongs on key_exact; ';

-- IDENTITY on any key would renumber the lookups and detach every foreign key.
IF EXISTS (
    SELECT 1 FROM sys.identity_columns ic
    JOIN sys.objects o ON o.object_id = ic.object_id
    WHERE o.schema_id = SCHEMA_ID('dbo'))
    SET @problems += N'an IDENTITY column exists; keys must be assigned by the loader; ';

-- case_serial must be BIGINT. INT overflows partway through the load.
IF NOT EXISTS (
    SELECT 1 FROM sys.columns c
    JOIN sys.objects o ON o.object_id = c.object_id
    JOIN sys.types  t ON t.user_type_id = c.user_type_id
    WHERE o.name = 'filings' AND c.name = 'case_serial' AND t.name = 'bigint')
    SET @problems += N'filings.case_serial is not BIGINT — the load will overflow; ';

-- Compatibility level 160+ or salary_trend's WINDOW clause is a syntax error,
-- and it looks like a bug in the query rather than a database setting.
IF (SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()) < 160
    SET @problems += N'compatibility level below 160 — salary_trend WINDOW will not parse; ';

IF @problems <> N''
    THROW 50000, @problems, 1;

PRINT 'schema ok: 6 tables, 2 indexes, 1 view, BIN2 confirmed';
GO
