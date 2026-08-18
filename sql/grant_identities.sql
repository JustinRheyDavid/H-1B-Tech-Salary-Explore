-- Grant the two managed identities access to sqldb-h1b.
--
-- Plan Step 6. This half of Step 6 is MANUAL and cannot be otherwise: database
-- users live inside the database, not in ARM, so no amount of Bicep will create
-- them. The storage half is declarative and lives in infra/roles.bicep.
--
-- RUN THIS AGAIN AFTER ANY DATABASE RECREATE, AND AFTER RECREATING EITHER THE
-- CONTAINER APP OR THE ETL JOB. Both cases break access, and they break it in
-- different ways:
--
--   database recreated  the users are gone with it, and `what-if` still reports
--                       "no change" because ARM never knew they existed.
--   app/job recreated   the users survive, but the identity behind them does
--                       not. A recreated app has the same NAME and a brand-new
--                       application ID, so the sid stored here now points at a
--                       principal that no longer exists.
--
-- Both surface as the same thing: `Login failed for user
-- '<token-identified principal>'`.
--
-- Run as the Entra admin (there is no SQL password — the server is
-- azureADOnlyAuthentication). THE `-b` IS NOT OPTIONAL:
--
--   sqlcmd -b -S sql-h1b-hutymqa65yoty.database.windows.net -d sqldb-h1b \
--     --authentication-method ActiveDirectoryAzCli -i sql/grant_identities.sql
--
-- Without `-b`, sqlcmd prints errors and still exits 0. A run where CREATE USER
-- failed and every grant after it failed too is indistinguishable, to the shell,
-- from a clean one. Verified: a script with two hard failures exited 0.
--
-- The database is serverless with autoPauseDelay 60. If it has been idle it is
-- paused, and the first connection fails with "Database ... is not currently
-- available" while it resumes. That is not an error to debug — wait and retry.
--
-- This script CONVERGES rather than skipping work: it drops and recreates both
-- users every run, so it ends in the correct state no matter what state it
-- started in. An earlier version guarded with `IF NOT EXISTS ... name = 'h1b-etl'`
-- and was subtly useless, because a name check cannot see a stale sid — the
-- recreated-app case above found the name, skipped, and left the broken user in
-- place while reporting success.
--
-- The whole thing is one transaction. Dropping a working user and then failing
-- to recreate it would be strictly worse than doing nothing, so either every
-- change below lands or none of them do.

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

BEGIN TRY
    BEGIN TRANSACTION;

    -- -----------------------------------------------------------------------
    -- h1b-etl — the ETL job. Reads, writes, and builds the schema.
    -- -----------------------------------------------------------------------
    --
    -- The name is the Container Apps job's resource name, which is also the
    -- display name of its system-assigned managed identity in Entra. FROM
    -- EXTERNAL PROVIDER makes the server resolve that name through Microsoft
    -- Graph, and re-resolving it on every run is what repairs a stale sid.
    --
    -- It stores the identity's APPLICATION (client) ID as the user's sid, NOT
    -- its object/principal ID. The two are different GUIDs for the same
    -- identity, and the runbook records both. Azure RBAC wants the object ID;
    -- Azure SQL wants the application ID. The assertions at the bottom of this
    -- script check the users exist, but nothing in SQL can check the sid is the
    -- CURRENT one — that is precisely why this drops and recreates rather than
    -- trying to detect staleness.
    DROP USER IF EXISTS [h1b-etl];
    CREATE USER [h1b-etl] FROM EXTERNAL PROVIDER;

    ALTER ROLE db_datareader ADD MEMBER [h1b-etl];
    ALTER ROLE db_datawriter ADD MEMBER [h1b-etl];

    -- Step 8's schema is created by the loader, so the ETL identity needs DDL
    -- rights: six tables, three indexes and the v_filings view.
    GRANT CREATE TABLE TO [h1b-etl];
    GRANT CREATE VIEW  TO [h1b-etl];

    -- DEVIATION FROM THE PLAN, and it is load-bearing. Plan Step 6 lists only
    -- `GRANT CREATE TABLE`, which is necessary but not sufficient: creating a
    -- table takes CREATE TABLE on the database AND ALTER on the schema it lands
    -- in. Verified by impersonating the user with only the plan's grants —
    --
    --   The specified schema name "dbo" either does not exist or you do not
    --   have permission to use it.
    --
    -- — which is a confusing way to be told about a missing permission, since
    -- the schema plainly does exist. Without this Step 8 fails on its first
    -- CREATE TABLE. ALTER ON SCHEMA::dbo rather than db_ddladmin or db_owner:
    -- it is the narrowest grant that lets the identity create objects in dbo.
    GRANT ALTER ON SCHEMA::dbo TO [h1b-etl];

    -- And REFERENCES, which ALTER does NOT imply. `filings` declares five
    -- foreign keys against the lookup tables, and creating a foreign key needs
    -- REFERENCES on the table being pointed at. Creating that table is not
    -- enough to get it: objects created in dbo are owned by the schema, not by
    -- the identity that ran the CREATE, so h1b-etl builds `employers` and is
    -- then refused permission to reference it. Observed exactly that with only
    -- ALTER granted —
    --
    --   The REFERENCES permission was denied on the object 'probe_parent'
    --
    -- — on a table the same identity had created one statement earlier.
    GRANT REFERENCES ON SCHEMA::dbo TO [h1b-etl];

    -- -----------------------------------------------------------------------
    -- h1b-web — the Streamlit dashboard. Read-only, deliberately.
    -- -----------------------------------------------------------------------
    --
    -- No db_datawriter, no DDL. The dashboard runs SELECTs and has no business
    -- writing to the database; if a query in queries.py ever needs to write,
    -- that is a bug in the query, and this grant is what surfaces it as one.
    -- The assertions below fail the script if it ever acquires write access.
    DROP USER IF EXISTS [h1b-web];
    CREATE USER [h1b-web] FROM EXTERNAL PROVIDER;

    ALTER ROLE db_datareader ADD MEMBER [h1b-web];

    COMMIT TRANSACTION;
    PRINT 'grants applied';
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
    -- Rethrow so sqlcmd -b sees a failure. Without this the CATCH would swallow
    -- the error and the script would end looking successful.
    THROW;
END CATCH
GO

-- ---------------------------------------------------------------------------
-- Fallback, if FROM EXTERNAL PROVIDER cannot resolve the name.
-- ---------------------------------------------------------------------------
--
-- It depends on the server being able to query Graph, which is not guaranteed
-- in every tenant. Deliberately NOT a ready-to-paste literal: the last version
-- of this comment hardcoded a sid, and a hardcoded sid goes stale the moment
-- the job is recreated — which is exactly when someone would reach for a
-- fallback. Pasting a stale one creates a user that looks perfect in
-- sys.database_principals and can never log in.
--
-- Derive it fresh. The sid is the APPLICATION id byte-swapped: reverse the
-- first three hyphen-separated groups, keep the last two as they are.
--
--   APPID=$(az ad sp list --display-name h1b-etl --query "[0].appId" -o tsv)
--   python3 -c "import uuid,sys; print('0x'+uuid.UUID(sys.argv[1]).bytes_le.hex().upper())" $APPID
--
-- Then, as the Entra admin:
--
--   DROP USER IF EXISTS [h1b-etl];
--   CREATE USER [h1b-etl] WITH SID = <the value above>, TYPE = E;
--
-- and re-apply every role and grant from the transaction above by hand.

-- ---------------------------------------------------------------------------
-- Assertions — Step 6's acceptance criterion, enforced rather than printed.
-- ---------------------------------------------------------------------------
--
-- The previous version ended with two SELECTs for a human to eyeball. Combined
-- with sqlcmd's exit code, that meant a half-applied script looked identical to
-- a clean one. These THROW instead, so `sqlcmd -b` exits non-zero and CI (or a
-- future Step 11 workflow) cannot go green on a broken grant.
DECLARE @problems nvarchar(max) = N'';

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'h1b-etl' AND type = 'E')
    SET @problems += N'h1b-etl user missing or not external; ';
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'h1b-web' AND type = 'E')
    SET @problems += N'h1b-web user missing or not external; ';

-- Role memberships, in both directions: h1b-etl must write, h1b-web must not.
IF NOT EXISTS (
    SELECT 1 FROM sys.database_role_members rm
    JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
    JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
    WHERE m.name = 'h1b-etl' AND r.name = 'db_datawriter')
    SET @problems += N'h1b-etl not in db_datawriter; ';
IF NOT EXISTS (
    SELECT 1 FROM sys.database_role_members rm
    JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
    JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
    WHERE m.name = 'h1b-web' AND r.name = 'db_datareader')
    SET @problems += N'h1b-web not in db_datareader; ';
IF EXISTS (
    SELECT 1 FROM sys.database_role_members rm
    JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
    JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
    WHERE m.name = 'h1b-web' AND r.name IN ('db_datawriter', 'db_ddladmin', 'db_owner'))
    SET @problems += N'h1b-web HAS WRITE ACCESS — it must be read-only; ';

-- The four grants Step 8 needs. Missing any one of them fails the schema build
-- with a message that does not name the missing permission.
IF NOT EXISTS (SELECT 1 FROM sys.database_permissions pe
               JOIN sys.database_principals pr ON pr.principal_id = pe.grantee_principal_id
               WHERE pr.name = 'h1b-etl' AND pe.permission_name = 'CREATE TABLE')
    SET @problems += N'h1b-etl missing CREATE TABLE; ';
IF NOT EXISTS (SELECT 1 FROM sys.database_permissions pe
               JOIN sys.database_principals pr ON pr.principal_id = pe.grantee_principal_id
               WHERE pr.name = 'h1b-etl' AND pe.permission_name = 'CREATE VIEW')
    SET @problems += N'h1b-etl missing CREATE VIEW; ';
IF NOT EXISTS (SELECT 1 FROM sys.database_permissions pe
               JOIN sys.database_principals pr ON pr.principal_id = pe.grantee_principal_id
               WHERE pr.name = 'h1b-etl' AND pe.permission_name = 'ALTER'
                 AND pe.class_desc = 'SCHEMA' AND SCHEMA_NAME(pe.major_id) = 'dbo')
    SET @problems += N'h1b-etl missing ALTER ON SCHEMA::dbo; ';
IF NOT EXISTS (SELECT 1 FROM sys.database_permissions pe
               JOIN sys.database_principals pr ON pr.principal_id = pe.grantee_principal_id
               WHERE pr.name = 'h1b-etl' AND pe.permission_name = 'REFERENCES'
                 AND pe.class_desc = 'SCHEMA' AND SCHEMA_NAME(pe.major_id) = 'dbo')
    SET @problems += N'h1b-etl missing REFERENCES ON SCHEMA::dbo; ';

IF @problems <> N''
    THROW 50000, @problems, 1;

PRINT 'all assertions passed';
GO

-- Informational. The sid printed here is the APPLICATION id, byte-swapped —
-- cross-check it against `az ad sp list --display-name h1b-etl --query "[0].appId"`
-- if a login is failing.
SELECT
    dp.name,
    dp.type_desc,
    CONVERT(varchar(100), dp.sid, 1) AS sid_is_the_application_id,
    STRING_AGG(r.name, ', ') WITHIN GROUP (ORDER BY r.name) AS roles
FROM sys.database_principals dp
LEFT JOIN sys.database_role_members rm ON rm.member_principal_id = dp.principal_id
LEFT JOIN sys.database_principals    r ON r.principal_id = rm.role_principal_id
WHERE dp.type = 'E'
GROUP BY dp.name, dp.type_desc, CONVERT(varchar(100), dp.sid, 1);
GO
