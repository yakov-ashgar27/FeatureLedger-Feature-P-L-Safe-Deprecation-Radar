-- FeatureLedger core schema (Phase 1, MVP v0.1)
-- Conventions
--   * Every tenant-owned row carries org_id; RLS (0003) scopes all access to current_setting('app.org_id').
--   * Money is NUMERIC(14,2) USD. Hours are NUMERIC(10,2).
--   * period_month is always the first day of a calendar month (CHECK enforced).
--   * PRIVACY BY SCHEMA: work_items has no author/reviewer/assignee columns. Individual developer
--     identity is scrubbed at ingestion (backend/app/ingest/scrub.py) and never persisted.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid(), digest()

-- ---------------------------------------------------------------- enums
CREATE TYPE user_role          AS ENUM ('admin', 'finance', 'pm', 'eng_lead', 'cs', 'viewer');
CREATE TYPE integration_kind   AS ENUM ('github', 'gitlab', 'jira', 'linear', 'posthog', 'mixpanel', 'amplitude',
                                        'stripe', 'hubspot', 'salesforce', 'zendesk', 'intercom',
                                        'aws_cost_explorer', 'gcp_billing', 'otel', 'llm_usage',
                                        'launchdarkly', 'statsig');
CREATE TYPE integration_status AS ENUM ('pending', 'active', 'error', 'disabled');
CREATE TYPE feature_status     AS ENUM ('active', 'candidate_for_sunset', 'sunsetting', 'archived');
CREATE TYPE allocation_rule    AS ENUM ('none', 'proportional_usage', 'proportional_cost', 'fixed_weights');
CREATE TYPE asset_type         AS ENUM ('path_glob', 'api_route', 'analytics_event', 'feature_flag',
                                        'jira_label', 'jira_epic', 'support_tag', 'cost_tag');
CREATE TYPE mapping_source     AS ENUM ('manifest', 'heuristic', 'ai', 'human');
CREATE TYPE mapping_status     AS ENUM ('confirmed', 'pending', 'rejected');
CREATE TYPE work_source        AS ENUM ('github', 'gitlab', 'jira', 'linear');
CREATE TYPE work_item_type     AS ENUM ('pr', 'issue');
CREATE TYPE work_kind          AS ENUM ('feature', 'bug', 'chore', 'hotfix', 'incident', 'revert');
CREATE TYPE account_health     AS ENUM ('green', 'yellow', 'red');
CREATE TYPE cost_type          AS ENUM ('eng', 'support', 'infra', 'llm');
CREATE TYPE feature_class      AS ENUM ('cash_cow', 'growth_bet', 'niche_anchor', 'zombie', 'watch',
                                        'mandated', 'platform');
CREATE TYPE sunset_state       AS ENUM ('candidate_for_sunset', 'blast_radius_checked', 'customer_outreach',
                                        'canary_dark_launch', 'dead_code_pr_opened', 'archived', 'cancelled');
CREATE TYPE triage_subject     AS ENUM ('work_item', 'support_ticket', 'code_asset');

-- ---------------------------------------------------------------- tenancy
CREATE TABLE organizations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{2,64}$'),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Financial parameters are typed columns (not JSONB) so bad values are rejected at write time.
CREATE TABLE org_settings (
    org_id                     uuid PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    eng_hourly_rate_usd        numeric(8,2)  NOT NULL DEFAULT 95.00  CHECK (eng_hourly_rate_usd > 0),
    support_hourly_rate_usd    numeric(8,2)  NOT NULL DEFAULT 45.00  CHECK (support_hourly_rate_usd > 0),
    hours_per_engineer_month   numeric(6,2)  NOT NULL DEFAULT 140.00 CHECK (hours_per_engineer_month BETWEEN 40 AND 220),
    allocatable_fraction       numeric(4,3)  NOT NULL DEFAULT 0.800  CHECK (allocatable_fraction BETWEEN 0.1 AND 1),
    calibration_clamp_max      numeric(5,2)  NOT NULL DEFAULT 4.00   CHECK (calibration_clamp_max >= 1),
    default_attribution_model  text          NOT NULL DEFAULT 'usage_weighted'
                               CHECK (default_attribution_model IN ('usage_weighted', 'tier_gated', 'critical_dependency')),
    critical_concentration     numeric(4,3)  NOT NULL DEFAULT 0.400  CHECK (critical_concentration BETWEEN 0.05 AND 1),
    anchor_min_arr_usd         numeric(14,2) NOT NULL DEFAULT 100000,
    tier_uplift_share          jsonb         NOT NULL DEFAULT '{"enterprise": 0.35, "pro": 0.20}',
    mapping_auto_confirm       numeric(3,2)  NOT NULL DEFAULT 0.80   CHECK (mapping_auto_confirm BETWEEN 0.5 AND 1),
    min_team_size_for_display  smallint      NOT NULL DEFAULT 3      CHECK (min_team_size_for_display >= 1),
    updated_at                 timestamptz   NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email       text NOT NULL,
    role        user_role NOT NULL DEFAULT 'viewer',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, email)
);

-- Teams carry headcount only (never a roster). Teams smaller than org_settings.min_team_size_for_display
-- are rolled up into parent_team_id in every engineering-cost view, so a 1-person team never exposes an individual.
CREATE TABLE teams (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    key             text NOT NULL,
    name            text NOT NULL,
    headcount       smallint NOT NULL CHECK (headcount >= 0),
    parent_team_id  uuid REFERENCES teams(id) ON DELETE SET NULL,
    UNIQUE (org_id, key)
);

-- ---------------------------------------------------------------- integrations & ingestion
CREATE TABLE integrations (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    kind              integration_kind NOT NULL,
    status            integration_status NOT NULL DEFAULT 'pending',
    display_name      text NOT NULL,
    config            jsonb NOT NULL DEFAULT '{}',          -- e.g. {"repos": ["acme/web"], "group_key": "company_id"}
    token_ciphertext  bytea,                                -- AES-256-GCM envelope (nonce||ciphertext||tag)
    token_key_id      text,                                 -- KMS key / local key id used for the envelope
    last_synced_at    timestamptz,
    last_error        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK ((token_ciphertext IS NULL) = (token_key_id IS NULL))
);
CREATE INDEX integrations_org_kind_idx ON integrations (org_id, kind);

CREATE TABLE sync_cursors (
    integration_id  uuid NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entity_type     text NOT NULL,
    cursor          text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (integration_id, entity_type)
);

-- Idempotent inbox: every connector writes scrubbed envelopes here first. The primary key IS the
-- idempotency key (sha256 of integration|entity|external_id|version) so replays are no-ops.
CREATE TABLE ingest_inbox (
    idempotency_key  text PRIMARY KEY,
    org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    integration_id   uuid NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    entity_type      text NOT NULL,
    external_id      text NOT NULL,
    payload          jsonb NOT NULL,
    received_at      timestamptz NOT NULL DEFAULT now(),
    processed_at     timestamptz,
    attempts         smallint NOT NULL DEFAULT 0,
    last_error       text
);
CREATE INDEX ingest_inbox_unprocessed_idx ON ingest_inbox (org_id, received_at) WHERE processed_at IS NULL;

CREATE TABLE repositories (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    integration_id  uuid REFERENCES integrations(id) ON DELETE SET NULL,
    full_name       text NOT NULL,                     -- "acme/web"
    default_branch  text NOT NULL DEFAULT 'main',
    manifest_sha    text,                              -- git blob sha of the features.yaml last applied
    UNIQUE (org_id, full_name)
);

-- ---------------------------------------------------------------- the feature graph
CREATE TABLE features (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    key                 text NOT NULL CHECK (key ~ '^[a-z0-9][a-z0-9-]{1,79}$'),
    name                text NOT NULL,
    description         text,
    status              feature_status NOT NULL DEFAULT 'active',
    owner_team_id       uuid REFERENCES teams(id) ON DELETE SET NULL,
    flag_key            text,
    tier_gate           text,                          -- 'pro' | 'enterprise' | NULL (all tiers)
    is_shared_overhead  boolean NOT NULL DEFAULT false,
    allocation_rule     allocation_rule NOT NULL DEFAULT 'none',
    allocation_weights  jsonb,                         -- only for fixed_weights: {"feature-key": 0.3, ...}
    is_regulatory       boolean NOT NULL DEFAULT false,-- GDPR export, audit log... never auto-sunset
    launched_at         date,
    metadata            jsonb NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, key),
    UNIQUE (org_id, id),
    CHECK (allocation_rule = 'none' OR is_shared_overhead),
    CHECK ((allocation_rule = 'fixed_weights') = (allocation_weights IS NOT NULL))
);
CREATE INDEX features_org_status_idx ON features (org_id, status);
CREATE UNIQUE INDEX features_org_flag_uidx ON features (org_id, flag_key) WHERE flag_key IS NOT NULL;

CREATE TABLE code_assets (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    repo_id     uuid REFERENCES repositories(id) ON DELETE CASCADE,
    asset_type  asset_type NOT NULL,
    identifier  text NOT NULL,                         -- glob, route prefix, event name, label...
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (org_id, repo_id, asset_type, identifier)
);

CREATE TABLE asset_feature_mappings (
    asset_id     uuid NOT NULL REFERENCES code_assets(id) ON DELETE CASCADE,
    feature_id   uuid NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source       mapping_source NOT NULL,
    confidence   numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status       mapping_status NOT NULL,
    decided_by   uuid REFERENCES users(id) ON DELETE SET NULL,
    decided_at   timestamptz,
    PRIMARY KEY (asset_id, feature_id),
    -- manifest & human mappings are authoritative; machine mappings are confirmed only above threshold
    CHECK (source IN ('manifest', 'human') OR status <> 'confirmed' OR confidence >= 0.50)
);
CREATE INDEX afm_feature_idx ON asset_feature_mappings (org_id, feature_id) WHERE status = 'confirmed';

-- Static import graph edges (metadata only, never source). Used for technical blast radius.
CREATE TABLE import_edges (
    org_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    repo_id    uuid NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    from_path  text NOT NULL,
    to_path    text NOT NULL,
    seen_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, from_path, to_path)
);
CREATE INDEX import_edges_to_idx ON import_edges (org_id, to_path);

-- ---------------------------------------------------------------- engineering effort
CREATE TABLE work_items (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                   uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source                   work_source NOT NULL,
    external_id              text NOT NULL,              -- "acme/web#1234"
    item_type                work_item_type NOT NULL,
    kind                     work_kind NOT NULL,
    priority                 text,                       -- 'P0'..'P4' when known
    team_id                  uuid REFERENCES teams(id) ON DELETE SET NULL,
    repo_id                  uuid REFERENCES repositories(id) ON DELETE SET NULL,
    opened_at                timestamptz NOT NULL,
    merged_at                timestamptz,
    period_month             date GENERATED ALWAYS AS (date_trunc('month', coalesce(merged_at, opened_at) AT TIME ZONE 'UTC')::date) STORED,
    additions                integer NOT NULL DEFAULT 0 CHECK (additions >= 0),
    deletions                integer NOT NULL DEFAULT 0 CHECK (deletions >= 0),
    files_changed            integer NOT NULL DEFAULT 0 CHECK (files_changed >= 0),
    review_rounds            smallint NOT NULL DEFAULT 0 CHECK (review_rounds >= 0),
    is_revert                boolean NOT NULL DEFAULT false,
    is_sweeping              boolean NOT NULL DEFAULT false,  -- >50 features or >5,000 lines: booked to overhead
    incident_response_hours  numeric(8,2) NOT NULL DEFAULT 0,  -- aggregate responders x duration, team-level
    raw_effort_hours         numeric(10,2),                     -- model output before team calibration
    path_stats               jsonb NOT NULL DEFAULT '[]',       -- [{"path": "...", "additions": n, "deletions": n}]
    linked_keys              text[] NOT NULL DEFAULT '{}',      -- issue keys parsed from title/body
    labels                   text[] NOT NULL DEFAULT '{}',
    created_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, source, external_id)
);
CREATE INDEX work_items_org_period_idx ON work_items (org_id, period_month, team_id);
COMMENT ON TABLE work_items IS 'PR/issue metadata. Deliberately has no author, reviewer or assignee column.';

-- How a work item's effort splits across features (shares sum to <= 1; remainder = unmapped).
CREATE TABLE work_item_allocations (
    work_item_id  uuid NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    feature_id    uuid NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    share         numeric(6,5) NOT NULL CHECK (share > 0 AND share <= 1),
    PRIMARY KEY (work_item_id, feature_id)
);
CREATE INDEX wia_feature_idx ON work_item_allocations (org_id, feature_id);

-- ---------------------------------------------------------------- customers & revenue
CREATE TABLE accounts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    external_ref  text NOT NULL,                      -- canonical id (e.g. Stripe customer)
    name          text NOT NULL,
    plan_tier     text NOT NULL,                      -- 'free' | 'starter' | 'pro' | 'enterprise'
    arr_usd       numeric(14,2) NOT NULL DEFAULT 0 CHECK (arr_usd >= 0),
    renewal_date  date,
    health        account_health NOT NULL DEFAULT 'green',
    csm_email     text,
    UNIQUE (org_id, external_ref)
);
CREATE INDEX accounts_org_arr_idx ON accounts (org_id, arr_usd DESC);

CREATE TABLE account_identities (
    account_id   uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    system       integration_kind NOT NULL,
    external_id  text NOT NULL,
    PRIMARY KEY (org_id, system, external_id)
);

-- Month-end revenue snapshot so historical P&L is not rewritten when a contract changes.
CREATE TABLE account_snapshots (
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    account_id    uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period_month  date NOT NULL CHECK (extract(day FROM period_month) = 1),
    arr_usd       numeric(14,2) NOT NULL CHECK (arr_usd >= 0),
    plan_tier     text NOT NULL,
    health        account_health NOT NULL,
    PRIMARY KEY (org_id, period_month, account_id)
);

-- ---------------------------------------------------------------- usage (time series, partitioned)
CREATE TABLE usage_signals (
    org_id        uuid NOT NULL,
    period_month  date NOT NULL CHECK (extract(day FROM period_month) = 1),
    account_id    uuid NOT NULL,
    feature_id    uuid NOT NULL,
    events        bigint  NOT NULL DEFAULT 0 CHECK (events >= 0),
    active_days   smallint NOT NULL DEFAULT 0 CHECK (active_days BETWEEN 0 AND 31),
    active_users  integer NOT NULL DEFAULT 0 CHECK (active_users >= 0),
    PRIMARY KEY (org_id, period_month, account_id, feature_id)
) PARTITION BY RANGE (period_month);

-- Distinct active days per account per month (a union, not a sum across features) -> concentration denominator.
CREATE TABLE account_activity_monthly (
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    period_month  date NOT NULL CHECK (extract(day FROM period_month) = 1),
    account_id    uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    active_days   smallint NOT NULL CHECK (active_days BETWEEN 0 AND 31),
    PRIMARY KEY (org_id, period_month, account_id)
);

-- ---------------------------------------------------------------- support
CREATE TABLE support_tickets (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    external_id         text NOT NULL,
    account_id          uuid REFERENCES accounts(id) ON DELETE SET NULL,
    feature_id          uuid REFERENCES features(id) ON DELETE SET NULL,
    mapping_source      mapping_source,
    mapping_confidence  numeric(3,2) CHECK (mapping_confidence BETWEEN 0 AND 1),
    priority            text,
    created_at          timestamptz NOT NULL,
    solved_at           timestamptz,
    handle_minutes      integer NOT NULL DEFAULT 0 CHECK (handle_minutes >= 0),
    period_month        date GENERATED ALWAYS AS (date_trunc('month', created_at AT TIME ZONE 'UTC')::date) STORED,
    UNIQUE (org_id, external_id)
);
CREATE INDEX support_tickets_feature_period_idx ON support_tickets (org_id, feature_id, period_month);

-- ---------------------------------------------------------------- the cost ledger (partitioned)
-- infra/llm rows come from billing connectors; eng/support rows are written by the P&L job
-- (source = 'featureledger.model', replaced atomically per org+month+model_version).
CREATE TABLE cost_events (
    id            bigint GENERATED ALWAYS AS IDENTITY,
    org_id        uuid NOT NULL,
    period_month  date NOT NULL CHECK (extract(day FROM period_month) = 1),
    feature_id    uuid,                              -- NULL = untagged spend (shown as a data-quality gap)
    cost_type     cost_type NOT NULL,
    source        text NOT NULL,                     -- 'aws_cost_explorer' | 'openai_usage' | 'featureledger.model' ...
    amount_usd    numeric(14,2) NOT NULL,
    quantity      numeric(18,4),
    unit          text,                              -- 'hours' | 'tokens' | 'usd'
    metadata      jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, period_month, id)
) PARTITION BY RANGE (period_month);
CREATE INDEX cost_events_lookup_idx ON cost_events (org_id, feature_id, period_month, cost_type);

-- ---------------------------------------------------------------- P&L outputs
CREATE TABLE feature_pnl_monthly (
    org_id                 uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    feature_id             uuid NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    period_month           date NOT NULL CHECK (extract(day FROM period_month) = 1),
    eng_hours              numeric(10,2) NOT NULL,
    bug_hours              numeric(10,2) NOT NULL,
    c_eng                  numeric(14,2) NOT NULL,
    c_support              numeric(14,2) NOT NULL,
    c_infra                numeric(14,2) NOT NULL,
    c_total                numeric(14,2) NOT NULL,      -- direct (contribution) cost
    c_overhead_alloc       numeric(14,2) NOT NULL,      -- allocated shared overhead (fully-loaded view only)
    v_usage                numeric(14,2) NOT NULL,      -- usage-weighted MRR (additive)
    v_tier                 numeric(14,2) NOT NULL,      -- tier-gated blended MRR (additive)
    revenue_at_risk_arr    numeric(14,2) NOT NULL,      -- counterfactual, NOT additive across features
    tickets                integer NOT NULL,
    active_accounts        integer NOT NULL,
    eligible_accounts      integer NOT NULL,         -- paying accounts on a tier that can use the feature
    anchor_accounts        integer NOT NULL,
    bug_tax_ratio          numeric(5,4) NOT NULL,
    classification         feature_class NOT NULL,
    classification_reason  text NOT NULL,
    model_version          text NOT NULL,
    computed_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, feature_id, period_month)
);
CREATE INDEX fpm_period_idx ON feature_pnl_monthly (org_id, period_month, classification);

-- Engineering capacity that could not be attributed (calibration clamp or unmapped churn): conservation check.
CREATE TABLE eng_capacity_monthly (
    org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    team_id             uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    period_month        date NOT NULL CHECK (extract(day FROM period_month) = 1),
    capacity_hours      numeric(10,2) NOT NULL,
    raw_hours           numeric(10,2) NOT NULL,
    calibration_factor  numeric(8,4) NOT NULL,
    feature_hours       numeric(10,2) NOT NULL,
    overhead_hours      numeric(10,2) NOT NULL,
    unmapped_hours      numeric(10,2) NOT NULL,
    unattributed_hours  numeric(10,2) NOT NULL,
    PRIMARY KEY (org_id, team_id, period_month)
);

CREATE TABLE account_dependencies (
    org_id               uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    feature_id           uuid NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    account_id           uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period_month         date NOT NULL CHECK (extract(day FROM period_month) = 1),
    arr_usd              numeric(14,2) NOT NULL,
    renewal_date         date,
    feature_active_days  smallint NOT NULL,
    account_active_days  smallint NOT NULL,
    dependency_score     numeric(5,4) NOT NULL CHECK (dependency_score BETWEEN 0 AND 1),
    churn_risk           numeric(5,4) NOT NULL CHECK (churn_risk BETWEEN 0 AND 1),
    attributed_mrr       numeric(14,2) NOT NULL,
    PRIMARY KEY (org_id, feature_id, period_month, account_id)
);
CREATE INDEX account_dep_risk_idx ON account_dependencies (org_id, period_month, feature_id, arr_usd DESC);

-- ---------------------------------------------------------------- triage (Tier 3 human-in-the-loop)
CREATE TABLE mapping_suggestions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    subject_type          triage_subject NOT NULL,
    subject_id            uuid NOT NULL,
    subject_label         text NOT NULL,             -- "acme/web#812 · fix pdf margin overflow" (no author)
    suggested_feature_id  uuid REFERENCES features(id) ON DELETE CASCADE,
    confidence            numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    source                mapping_source NOT NULL CHECK (source IN ('heuristic', 'ai')),
    rationale             text NOT NULL,
    status                mapping_status NOT NULL DEFAULT 'pending',
    decided_feature_id    uuid REFERENCES features(id) ON DELETE SET NULL,
    decided_by            uuid REFERENCES users(id) ON DELETE SET NULL,
    decided_at            timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, subject_type, subject_id)
);
CREATE INDEX mapping_suggestions_queue_idx ON mapping_suggestions (org_id, status, confidence DESC);

-- ---------------------------------------------------------------- safe sunset
CREATE TABLE sunset_plans (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    feature_id         uuid NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    state              sunset_state NOT NULL DEFAULT 'candidate_for_sunset',
    canary_phase       text CHECK (canary_phase IN ('zero_usage_off', 'fully_off')),
    scream_test_days   smallint NOT NULL DEFAULT 30 CHECK (scream_test_days BETWEEN 7 AND 180),
    reason             text NOT NULL,
    blast_radius       jsonb,
    impact_dossier     jsonb,
    canary             jsonb,
    cleanup_pr         jsonb,
    created_by         uuid REFERENCES users(id) ON DELETE SET NULL,
    state_entered_at   timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    version            integer NOT NULL DEFAULT 1        -- optimistic concurrency for transitions
);
-- at most one live plan per feature
CREATE UNIQUE INDEX sunset_plans_one_live_uidx ON sunset_plans (org_id, feature_id)
    WHERE state NOT IN ('archived', 'cancelled');

-- Append-only, hash-chained audit trail.
CREATE TABLE sunset_audit_logs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    plan_id     uuid NOT NULL REFERENCES sunset_plans(id) ON DELETE CASCADE,
    from_state  sunset_state,
    to_state    sunset_state NOT NULL,
    action      text NOT NULL,                         -- 'transition' | 'rollback' | 'cancel' | 'note'
    actor_id    uuid REFERENCES users(id) ON DELETE SET NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    prev_hash   bytea,
    hash        bytea NOT NULL
);
CREATE INDEX sunset_audit_plan_idx ON sunset_audit_logs (plan_id, id);

CREATE FUNCTION sunset_audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    last_hash bytea;
BEGIN
    SELECT hash INTO last_hash FROM sunset_audit_logs
     WHERE plan_id = NEW.plan_id ORDER BY id DESC LIMIT 1;
    NEW.prev_hash := last_hash;
    NEW.hash := digest(coalesce(encode(last_hash, 'hex'), '') || '|' || NEW.plan_id::text || '|' ||
                       coalesce(NEW.from_state::text, '') || '|' || NEW.to_state::text || '|' ||
                       NEW.action || '|' || NEW.payload::text || '|' || NEW.created_at::text, 'sha256');
    RETURN NEW;
END $$;
CREATE TRIGGER sunset_audit_chain_trg BEFORE INSERT ON sunset_audit_logs
    FOR EACH ROW EXECUTE FUNCTION sunset_audit_chain();

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'sunset_audit_logs is append-only';
END $$;
CREATE TRIGGER sunset_audit_immutable_trg BEFORE UPDATE OR DELETE ON sunset_audit_logs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- monthly partitions
CREATE FUNCTION ensure_month_partitions(parent regclass, from_month date, to_month date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    m date := date_trunc('month', from_month)::date;
    part text;
BEGIN
    WHILE m <= to_month LOOP
        part := format('%s_%s', parent::text, to_char(m, 'YYYY_MM'));
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
                       part, parent::text, m, (m + interval '1 month')::date);
        m := (m + interval '1 month')::date;
    END LOOP;
END $$;

SELECT ensure_month_partitions('usage_signals', '2025-01-01', '2027-12-01');
SELECT ensure_month_partitions('cost_events',   '2025-01-01', '2027-12-01');
CREATE TABLE usage_signals_default PARTITION OF usage_signals DEFAULT;
CREATE TABLE cost_events_default   PARTITION OF cost_events   DEFAULT;
