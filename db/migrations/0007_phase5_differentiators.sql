-- Phase 5: AI gross margin + token-bleed radar, Zombie-Guard PR bot, executive digest, live demo org.

-- ---------------------------------------------------------------- AI gross margin
-- c_infra now holds fixed cloud infra only; variable LLM/AI token spend moves to c_ai_tokens.
-- c_total = c_eng + c_support + c_infra + c_ai_tokens (unchanged in total).
ALTER TABLE feature_pnl_monthly
    ADD COLUMN c_ai_tokens          numeric(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN gross_margin_pct     numeric(10,4),               -- (v_usage - c_ai_tokens - c_infra) / max(v_usage, 1)
    ADD COLUMN margin_bleed         boolean NOT NULL DEFAULT false,
    ADD COLUMN margin_bleed_reason  text,
    ADD COLUMN ai_tier_breakdown    jsonb NOT NULL DEFAULT '{}'; -- tier -> {tokens_usd, revenue_usd, accounts, events}
CREATE INDEX fpm_margin_bleed_idx ON feature_pnl_monthly (org_id, period_month) WHERE margin_bleed;

ALTER TABLE org_settings
    ADD COLUMN margin_bleed_min_adoption     numeric(4,3) NOT NULL DEFAULT 0.150 CHECK (margin_bleed_min_adoption BETWEEN 0 AND 1),
    ADD COLUMN margin_bleed_max_gross_margin numeric(4,3) NOT NULL DEFAULT 0.200 CHECK (margin_bleed_max_gross_margin BETWEEN -1 AND 1);

-- What a sunset reclaimed, frozen at archive time (archived features leave the ledger afterwards).
ALTER TABLE sunset_plans ADD COLUMN reclaimed jsonb;

-- ---------------------------------------------------------------- Zombie-Guard
-- One bot comment per pull request: created on `opened`, edited in place on `synchronize`.
CREATE TABLE pr_guard_comments (
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    repo          text NOT NULL,
    pr_number     integer NOT NULL,
    provider      text NOT NULL CHECK (provider IN ('sandbox', 'github')),
    comment_id    bigint NOT NULL,
    head_sha      text,
    body          text NOT NULL,          -- our own markdown (feature names + numbers; no code, no people)
    body_sha      text NOT NULL,
    features      jsonb NOT NULL DEFAULT '[]',
    flagged       boolean NOT NULL,
    updates       integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, repo, pr_number)
);

-- ---------------------------------------------------------------- executive digest
CREATE TABLE executive_digests (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    period_month  date NOT NULL CHECK (extract(day FROM period_month) = 1),
    trigger       text NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    summary       jsonb NOT NULL,
    slack_blocks  jsonb NOT NULL,
    slack_status  text NOT NULL CHECK (slack_status IN ('posted', 'dry_run', 'failed', 'skipped')),
    slack_error   text,
    created_by    uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX executive_digests_org_idx ON executive_digests (org_id, created_at DESC);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['pr_guard_comments', 'executive_digests'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
                          WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid)$p$, t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------- live demo org
ALTER TABLE organizations ADD COLUMN is_demo boolean NOT NULL DEFAULT false;

-- Resetting the demo org deletes it (cascade) and re-seeds it. The audit log stays append-only for
-- everyone else: a delete is allowed only for the org named in featureledger.purge_org, only when that
-- org is a demo org, and never for the RLS-bound application role.
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_user <> 'featureledger_app'
       AND current_setting('featureledger.purge_org', true) = OLD.org_id::text
       AND EXISTS (SELECT 1 FROM organizations o WHERE o.id = OLD.org_id AND o.is_demo) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'sunset_audit_logs is append-only';
END $$;
