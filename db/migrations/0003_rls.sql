-- Tenant isolation. The API connects as the non-owner role `featureledger_app` (created by
-- backend/app/migrate.py) and sets `SET LOCAL app.org_id = '<uuid>'` at the start of every
-- transaction. With FORCE ROW LEVEL SECURITY even accidental cross-tenant queries return nothing.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'org_settings', 'users', 'teams', 'integrations', 'sync_cursors', 'ingest_inbox', 'repositories',
        'features', 'code_assets', 'asset_feature_mappings', 'import_edges', 'work_items',
        'work_item_allocations', 'accounts', 'account_identities', 'account_snapshots', 'usage_signals',
        'account_activity_monthly', 'support_tickets', 'cost_events', 'feature_pnl_monthly',
        'eng_capacity_monthly', 'account_dependencies', 'mapping_suggestions', 'sunset_plans',
        'sunset_audit_logs'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
                          WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid)$p$, t);
    END LOOP;

    IF to_regclass('feature_embeddings') IS NOT NULL THEN
        FOREACH t IN ARRAY ARRAY['feature_embeddings', 'subject_embeddings'] LOOP
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
            EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                              USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
                              WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid)$p$, t);
        END LOOP;
    END IF;
END $$;

-- organizations: an app session may only see its own org row.
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON organizations
    USING (id = nullif(current_setting('app.org_id', true), '')::uuid);

-- The only cross-tenant lookup the app may do: slug -> id, to start a tenant-scoped session.
-- SECURITY DEFINER runs as the migration owner (bypasses RLS) but returns nothing but the id.
CREATE FUNCTION resolve_org_slug(p_slug text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT id FROM organizations WHERE slug = p_slug $$;
REVOKE ALL ON FUNCTION resolve_org_slug(text) FROM PUBLIC;
