-- Tenant isolation for partitions.
--
-- Found by the isolation audit: RLS policies on a partitioned parent are NOT applied when a partition is
-- queried by name, and the blanket "GRANT ... ON ALL TABLES" gave the application role SELECT on every
-- partition. `SELECT * FROM usage_signals_2026_10` (no tenant set) returned another tenant's rows.
-- The application never names partitions, so this needed SQL injection to exploit, but isolation must
-- not depend on that. Two independent layers now close it:
--   1. the application role has no privileges at all on partitions (it reaches them through the parent,
--      which is the only table whose privileges PostgreSQL checks for such access);
--   2. every partition also carries the same FORCEd tenant policy as its parent.

CREATE OR REPLACE FUNCTION secure_partition(part regclass) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', part);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', part);
    IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = part AND polname = 'tenant_isolation') THEN
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %s
                          USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
                          WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid)$p$, part);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featureledger_app') THEN
        EXECUTE format('REVOKE ALL ON %s FROM featureledger_app', part);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION ensure_month_partitions(parent regclass, from_month date, to_month date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    m date := date_trunc('month', from_month)::date;
    part text;
BEGIN
    WHILE m <= to_month LOOP
        part := format('%s_%s', parent::text, to_char(m, 'YYYY_MM'));
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
                       part, parent::text, m, (m + interval '1 month')::date);
        PERFORM secure_partition(part::regclass);
        m := (m + interval '1 month')::date;
    END LOOP;
END $$;

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT c.oid::regclass AS part
               FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid
              WHERE c.relkind = 'r' AND c.relispartition AND c.relnamespace = 'public'::regnamespace LOOP
        PERFORM secure_partition(r.part);
    END LOOP;
END $$;
