-- Phase 2: live connectors, scheduled syncs, dead letters, identity stitching.

-- Inbox: dead-letter isolation (a poison envelope is parked after 5 failed attempts and never
-- blocks its stream) and fast "latest version of an entity" lookups for rebuild-style processors.
ALTER TABLE ingest_inbox
    ADD COLUMN external_version text,
    ADD COLUMN dead_lettered_at timestamptz;
CREATE INDEX ingest_inbox_entity_idx ON ingest_inbox (org_id, entity_type, external_id, received_at DESC);
CREATE INDEX ingest_inbox_pending_idx ON ingest_inbox (org_id, entity_type)
    WHERE processed_at IS NULL AND dead_lettered_at IS NULL;
CREATE INDEX ingest_inbox_dead_idx ON ingest_inbox (org_id, integration_id) WHERE dead_lettered_at IS NOT NULL;

-- One integration per kind per org; sandbox flag selects fixture transports instead of live APIs.
CREATE UNIQUE INDEX integrations_org_kind_uidx ON integrations (org_id, kind);
ALTER TABLE integrations
    ADD COLUMN sandbox boolean NOT NULL DEFAULT false,
    ADD COLUMN sync_interval_minutes smallint NOT NULL DEFAULT 15 CHECK (sync_interval_minutes >= 5);

-- Identity stitching: email-domain fallback and Zendesk organization reference for re-stitching.
ALTER TABLE accounts ADD COLUMN email_domain text;
CREATE INDEX accounts_domain_idx ON accounts (org_id, email_domain) WHERE email_domain IS NOT NULL;
ALTER TABLE support_tickets ADD COLUMN external_org_ref text;
ALTER TABLE support_tickets ADD COLUMN handle_time_estimated boolean NOT NULL DEFAULT false;

-- Every sync attempt, for the Sync Health view.
CREATE TYPE sync_status AS ENUM ('running', 'succeeded', 'partial', 'failed');
CREATE TABLE sync_runs (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    integration_id      uuid NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    trigger             text NOT NULL CHECK (trigger IN ('schedule', 'manual', 'webhook', 'startup')),
    status              sync_status NOT NULL DEFAULT 'running',
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    batches             integer NOT NULL DEFAULT 0,
    envelopes_seen      integer NOT NULL DEFAULT 0,
    envelopes_new       integer NOT NULL DEFAULT 0,
    processed           integer NOT NULL DEFAULT 0,
    dead_lettered       integer NOT NULL DEFAULT 0,
    error               text
);
CREATE INDEX sync_runs_recent_idx ON sync_runs (org_id, integration_id, started_at DESC);

ALTER TABLE sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sync_runs
    USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
    WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid);

-- The scheduler's only cross-tenant read: which integrations exist and which are due.
CREATE FUNCTION list_sync_targets() RETURNS TABLE (org_id uuid, integration_id uuid, kind integration_kind,
                                                    sandbox boolean, due boolean)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT i.org_id, i.id, i.kind, i.sandbox,
          (i.last_synced_at IS NULL OR i.last_synced_at < now() - make_interval(mins => i.sync_interval_minutes - 1))
     FROM integrations i
    WHERE i.status <> 'disabled' AND i.kind <> 'github' AND (i.sandbox OR i.status <> 'pending') $$;
REVOKE ALL ON FUNCTION list_sync_targets() FROM PUBLIC;
