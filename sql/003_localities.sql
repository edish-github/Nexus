-- Enforce row-level locality on the two tables that need it.
--
-- 001_schema.sql declares `LOCALITY REGIONAL BY ROW` on incidents and
-- playbooks, but `CREATE TABLE IF NOT EXISTS` ignores the clause entirely when
-- the table already exists. Any cluster where those tables were created before
-- `ALTER DATABASE ... ADD REGION` ran ends up with the default locality
-- (REGIONAL BY TABLE IN PRIMARY REGION) and no crdb_region column, which breaks
-- per-row homing and the region-failure behaviour the schema assumes.
--
-- `SET LOCALITY` is idempotent, so this file is safe to re-run. schema_locked
-- is cleared around each change because CockroachDB refuses schema changes on a
-- locked descriptor, then restored.

ALTER TABLE incidents SET (schema_locked = false);
ALTER TABLE incidents SET LOCALITY REGIONAL BY ROW;
ALTER TABLE incidents SET (schema_locked = true);

ALTER TABLE playbooks SET (schema_locked = false);
ALTER TABLE playbooks SET LOCALITY REGIONAL BY ROW;
ALTER TABLE playbooks SET (schema_locked = true);
