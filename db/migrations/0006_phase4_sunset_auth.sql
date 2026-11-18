-- Phase 4: automated Safe Sunset execution (live flags, cleanup PRs) + enterprise auth (OIDC SSO + RBAC).

-- ---------------------------------------------------------------- roles
-- The four Phase 4 roles. Legacy values (finance, pm, eng_lead, cs) stay valid and are mapped in
-- app/auth/rbac.py: pm/eng_lead -> pm_tech_lead, cs -> cs_lead, finance -> viewer.
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'pm_tech_lead';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'cs_lead';

ALTER TABLE users
    ADD COLUMN name           text,
    ADD COLUMN oidc_issuer    text,
    ADD COLUMN oidc_subject   text,
    ADD COLUMN last_login_at  timestamptz,
    ADD COLUMN disabled       boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX users_oidc_uidx ON users (oidc_issuer, oidc_subject) WHERE oidc_subject IS NOT NULL;

-- ---------------------------------------------------------------- auth lookups (pre-tenant)
-- Authentication happens before a tenant context exists, so these three narrow SECURITY DEFINER
-- functions are the only way the app role can read a user outside RLS. They return one user, never a list.
CREATE FUNCTION auth_get_user(p_user_id uuid)
RETURNS TABLE (id uuid, org_id uuid, org_slug text, email text, name text, role text, disabled boolean)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT u.id, u.org_id, o.slug, u.email, u.name, u.role::text, u.disabled
     FROM users u JOIN organizations o ON o.id = u.org_id WHERE u.id = p_user_id $$;

-- dev login (SANDBOX_MODE only, enforced in the API): first enabled user of a role, or a user by email
CREATE FUNCTION auth_dev_user(p_org_slug text, p_role text, p_email text)
RETURNS TABLE (id uuid, org_id uuid, org_slug text, email text, name text, role text, disabled boolean)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT u.id, u.org_id, o.slug, u.email, u.name, u.role::text, u.disabled
     FROM users u JOIN organizations o ON o.id = u.org_id
    WHERE o.slug = p_org_slug AND NOT u.disabled
      AND (p_email IS NULL OR lower(u.email) = lower(p_email))
      AND (p_role IS NULL OR u.role::text = p_role)
    ORDER BY u.created_at, u.email LIMIT 1 $$;

-- OIDC callback: find by (issuer, subject), else by email; create as p_role (viewer by default).
-- The IdP role only overwrites the stored role when the IdP is authoritative (p_idp_role = true).
CREATE FUNCTION auth_upsert_user(p_org_slug text, p_email text, p_name text, p_issuer text, p_subject text,
                                 p_role text, p_idp_role boolean)
RETURNS TABLE (id uuid, org_id uuid, org_slug text, email text, name text, role text, disabled boolean)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS
$$
#variable_conflict use_column
DECLARE
    v_org uuid;
    v_id  uuid;
BEGIN
    SELECT o.id INTO v_org FROM organizations o WHERE o.slug = p_org_slug;
    IF v_org IS NULL THEN
        RAISE EXCEPTION 'unknown organization %', p_org_slug;
    END IF;
    SELECT u.id INTO v_id FROM users u
     WHERE u.org_id = v_org AND ((u.oidc_issuer = p_issuer AND u.oidc_subject = p_subject)
                                 OR lower(u.email) = lower(p_email))
     ORDER BY (u.oidc_subject = p_subject) DESC NULLS LAST LIMIT 1;
    IF v_id IS NULL THEN
        INSERT INTO users (org_id, email, name, role, oidc_issuer, oidc_subject, last_login_at)
        VALUES (v_org, lower(p_email), p_name, p_role::user_role, p_issuer, p_subject, now())
        RETURNING users.id INTO v_id;
    ELSE
        UPDATE users u SET name = coalesce(p_name, u.name), oidc_issuer = p_issuer, oidc_subject = p_subject,
                           last_login_at = now(),
                           role = CASE WHEN p_idp_role THEN p_role::user_role ELSE u.role END
         WHERE u.id = v_id;
    END IF;
    RETURN QUERY SELECT * FROM auth_get_user(v_id);
END $$;

-- Webhooks carry no user: resolve the tenant from the integration id in the URL (then verify the signature).
CREATE FUNCTION integration_org(p_integration_id uuid) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT org_id FROM integrations WHERE id = p_integration_id $$;

REVOKE ALL ON FUNCTION auth_get_user(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_dev_user(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_upsert_user(text, text, text, text, text, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION integration_org(uuid) FROM PUBLIC;

-- ---------------------------------------------------------------- live feature flags
-- Desired + last-applied state of every flag FeatureLedger controls.
--   fully_on        everyone gets the feature (initial state; rollback target)
--   zero_usage_off  off for every account with 0 events in the last 90 days; keep_on_for stays on
--   fully_off       off for everyone: the scream test is running
CREATE TABLE flag_states (
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    flag_key      text NOT NULL,
    provider      text NOT NULL CHECK (provider IN ('sandbox', 'launchdarkly', 'statsig')),
    state         text NOT NULL CHECK (state IN ('fully_on', 'zero_usage_off', 'fully_off')),
    keep_on_for   jsonb NOT NULL DEFAULT '[]',     -- account keys still served 'on' in zero_usage_off
    plan_id       uuid REFERENCES sunset_plans(id) ON DELETE SET NULL,
    provider_ref  jsonb NOT NULL DEFAULT '{}',     -- e.g. LaunchDarkly version, Statsig gate id
    last_error    text,
    updated_by    uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, flag_key)
);

-- Hourly evaluation counts per flag (pulled from LaunchDarkly, pushed by Statsig webhooks, or sandbox).
CREATE TABLE flag_evaluations (
    org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    flag_key      text NOT NULL,
    bucket_start  timestamptz NOT NULL,
    source        text NOT NULL,
    on_count      bigint NOT NULL DEFAULT 0 CHECK (on_count >= 0),
    off_count     bigint NOT NULL DEFAULT 0 CHECK (off_count >= 0),
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, flag_key, bucket_start, source)
);

-- ---------------------------------------------------------------- automated cleanup PRs
CREATE TABLE cleanup_pull_requests (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    plan_id        uuid NOT NULL REFERENCES sunset_plans(id) ON DELETE CASCADE,
    provider       text NOT NULL CHECK (provider IN ('sandbox', 'github')),
    repo           text NOT NULL,
    number         integer NOT NULL,
    url            text,
    branch         text NOT NULL,
    base_branch    text NOT NULL,
    head_sha       text,
    state          text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'merged', 'closed')),
    files_deleted  jsonb NOT NULL DEFAULT '[]',
    files_edited   jsonb NOT NULL DEFAULT '[]',
    created_at     timestamptz NOT NULL DEFAULT now(),
    merged_at      timestamptz,
    merge_sha      text,
    UNIQUE (org_id, repo, number)
);
CREATE INDEX cleanup_prs_plan_idx ON cleanup_pull_requests (plan_id);

ALTER TABLE sunset_plans ADD COLUMN rollback_count integer NOT NULL DEFAULT 0 CHECK (rollback_count >= 0);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['flag_states', 'flag_evaluations', 'cleanup_pull_requests'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
                          WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid)$p$, t);
    END LOOP;
END $$;
