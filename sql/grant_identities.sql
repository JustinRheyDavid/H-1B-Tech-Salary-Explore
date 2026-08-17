-- Grant the two managed identities access to sqldb-h1b.
--
-- Plan Step 6. This half of Step 6 is MANUAL and cannot be otherwise: database
-- users live inside the database, not in ARM, so no amount of Bicep will create
-- them. The storage half is declarative and lives in infra/roles.bicep.
--
-- RUN THIS AGAIN AFTER ANY DATABASE RECREATE. Dropping and redeploying the
-- database drops its users with it, `az deployment group what-if` will report
-- "no change" because ARM knows nothing about them, and the first symptom is
-- the ETL job failing with:
--
--     Login failed for user '<token-identified principal>'
--
-- Run as the Entra admin (there is no SQL password — the server is
-- azureADOnlyAuthentication):
--
--   sqlcmd -S sql-h1b-hutymqa65yoty.database.windows.net -d sqldb-h1b \
--     --authentication-method ActiveDirectoryAzCli -i sql/grant_identities.sql
--
-- The database is serverless with autoPauseDelay 60. If it has been idle it is
-- paused, and the first connection fails with "Database ... is not currently
-- available" while it resumes. That is not an error to debug — wait and retry.
--
-- Re-runnable by design. Every statement is guarded, so running it twice is a
-- no-op rather than "User, group, or role already exists in the current
-- database" halfway through a script that then leaves the grants half-applied.

SET NOCOUNT ON;
GO

-- ---------------------------------------------------------------------------
-- h1b-etl — the ETL job. Reads, writes, and builds the schema.
-- ---------------------------------------------------------------------------

-- The name here is the Container Apps job's resource name, which is also the
-- display name of its system-assigned managed identity in Entra. FROM EXTERNAL
-- PROVIDER makes the server resolve that name through Microsoft Graph.
--
-- It stores the identity's APPLICATION (client) ID as the user's sid, NOT its
-- object/principal ID. The two are different GUIDs for the same identity, and
-- the runbook records both for this reason. It matters if you ever fall back to
-- the explicit form below: a user created with the object ID is created
-- successfully, looks right in sys.database_principals, and can never log in,
-- because the token the identity presents carries the application ID.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'h1b-etl')
    CREATE USER [h1b-etl] FROM EXTERNAL PROVIDER;
GO

-- Fallback if FROM EXTERNAL PROVIDER fails to resolve the name — it depends on
-- the server being able to query Graph, which is not guaranteed in every tenant.
-- The application ID comes from:
--     az ad sp list --display-name h1b-etl --query "[].appId" -o tsv
-- and must be byte-swapped into a SQL sid: reverse the first three
-- hyphen-separated groups, keep the last two as they are.
--
--   CREATE USER [h1b-etl] WITH SID = 0xEA8B9706BB226B418CE63CD1C212E0F0, TYPE = E;

ALTER ROLE db_datareader ADD MEMBER [h1b-etl];
ALTER ROLE db_datawriter ADD MEMBER [h1b-etl];
GO

-- Step 8's schema is created by the loader, so the ETL identity needs DDL
-- rights: six tables, two indexes and the v_filings view.
GRANT CREATE TABLE TO [h1b-etl];
GRANT CREATE VIEW  TO [h1b-etl];
GO

-- DEVIATION FROM THE PLAN, and it is load-bearing. Plan Step 6 lists only
-- `GRANT CREATE TABLE`, which is necessary but not sufficient: creating a table
-- takes CREATE TABLE on the database AND ALTER on the schema it lands in.
-- Verified by impersonating the user with only the plan's grants —
--
--   The specified schema name "dbo" either does not exist or you do not have
--   permission to use it.
--
-- — which is a confusing way to be told about a missing permission, since the
-- schema plainly does exist. Without this line Step 8 fails on its first
-- CREATE TABLE.
--
-- ALTER ON SCHEMA::dbo rather than db_ddladmin or db_owner: it is the narrowest
-- grant that lets the identity create objects in dbo.
GRANT ALTER ON SCHEMA::dbo TO [h1b-etl];
GO

-- And REFERENCES, which ALTER does NOT imply. `filings` declares five foreign
-- keys against the lookup tables, and creating a foreign key needs REFERENCES on
-- the table being pointed at. Creating that table is not enough to get it:
-- objects created in dbo are owned by the schema, not by the identity that ran
-- the CREATE, so h1b-etl builds `employers` and is then refused permission to
-- reference it. Observed exactly that with only ALTER granted —
--
--   The REFERENCES permission was denied on the object 'probe_parent'
--
-- — on a table the same identity had created one statement earlier.
GRANT REFERENCES ON SCHEMA::dbo TO [h1b-etl];
GO

-- ---------------------------------------------------------------------------
-- h1b-web — the Streamlit dashboard. Read-only, deliberately.
-- ---------------------------------------------------------------------------
--
-- No db_datawriter, no DDL. The dashboard runs SELECTs and has no business
-- writing to the database; if a query in queries.py ever needs to write, that is
-- a bug in the query, and this grant is what surfaces it as one.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'h1b-web')
    CREATE USER [h1b-web] FROM EXTERNAL PROVIDER;
GO

ALTER ROLE db_datareader ADD MEMBER [h1b-web];
GO

-- ---------------------------------------------------------------------------
-- Verification — Step 6's acceptance criterion.
-- ---------------------------------------------------------------------------
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

SELECT
    pr.name,
    pe.permission_name,
    pe.class_desc,
    ISNULL(SCHEMA_NAME(pe.major_id), '') AS on_schema
FROM sys.database_permissions pe
JOIN sys.database_principals  pr ON pr.principal_id = pe.grantee_principal_id
WHERE pr.type = 'E'
ORDER BY pr.name, pe.permission_name;
GO
