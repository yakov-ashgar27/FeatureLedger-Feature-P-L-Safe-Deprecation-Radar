-- Phase 3: intelligent Feature Graph and automated manifest sync.

-- Where each slice of a PR's effort came from, so precedence can be enforced on re-allocation:
-- human > manifest > auto-confirmed machine (heuristic = Tier 2, ai = Tier 3).
ALTER TABLE work_item_allocations ADD COLUMN source mapping_source NOT NULL DEFAULT 'manifest';
CREATE INDEX wia_human_idx ON work_item_allocations (work_item_id) WHERE source = 'human';

-- Every features.yaml we have seen (GitHub push, disk, API), applied or rejected. The active manifest is
-- the latest applied row; an invalid push is recorded with its errors and changes nothing else.
CREATE TABLE manifest_versions (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source        text NOT NULL CHECK (source IN ('github_push', 'disk', 'api')),
    repo          text,
    path          text,
    commit_sha    text,
    status        text NOT NULL CHECK (status IN ('applied', 'invalid', 'unchanged', 'removed')),
    errors        jsonb NOT NULL DEFAULT '[]',
    content       text,                        -- the manifest itself (our config, no customer data)
    content_sha   text,
    summary       jsonb NOT NULL DEFAULT '{}', -- what apply changed: teams, features, mappings
    received_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX manifest_versions_active_idx ON manifest_versions (org_id, received_at DESC) WHERE status = 'applied';

ALTER TABLE manifest_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE manifest_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON manifest_versions
    USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
    WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid);

-- Triage queue: which tier proposed it and the evidence behind the confidence.
ALTER TABLE mapping_suggestions
    ADD COLUMN tier smallint CHECK (tier IN (2, 3)),
    ADD COLUMN evidence jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN auto_confirmed boolean NOT NULL DEFAULT false,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX mapping_suggestions_tier_idx ON mapping_suggestions (org_id, tier, status);

-- The graph job (hourly + after syncs and pushes) needs to enumerate tenants.
CREATE FUNCTION list_orgs() RETURNS TABLE (org_id uuid, slug text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT id, slug FROM organizations $$;
REVOKE ALL ON FUNCTION list_orgs() FROM PUBLIC;
