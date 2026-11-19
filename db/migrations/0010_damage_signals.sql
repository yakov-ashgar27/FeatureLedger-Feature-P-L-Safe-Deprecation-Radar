-- Sunset damage signals: production evidence that switching a feature off hurt someone.
--
--   error            an application error attributed to the feature (Sentry, or any error pipeline)
--   deprecated_call  a request to one of the feature's API routes (API gateway / CDN / access logs)
--
-- During the scream test, any signal that is not dismissed blocks the next step, the same way a support
-- ticket or an 'on' flag evaluation does. Titles are redacted before they are stored (no emails or
-- long numbers) and routes are stored without query strings and with numeric ids replaced.

CREATE TABLE damage_signals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    feature_id      uuid REFERENCES features(id) ON DELETE CASCADE,        -- NULL: matched no feature
    source          text NOT NULL CHECK (source IN ('sentry', 'generic')),
    kind            text NOT NULL CHECK (kind IN ('error', 'deprecated_call')),
    external_id     text NOT NULL,
    title           text,
    route           text,
    matched_by      text NOT NULL CHECK (matched_by IN ('tag', 'route', 'path', 'none')),
    occurrences     integer NOT NULL DEFAULT 1 CHECK (occurrences > 0),
    occurred_at     timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    dismissed_at    timestamptz,
    dismissed_by    text,
    dismiss_reason  text,
    UNIQUE (org_id, source, external_id)
);
CREATE INDEX damage_signals_live_idx ON damage_signals (org_id, feature_id, occurred_at) WHERE dismissed_at IS NULL;

ALTER TABLE damage_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE damage_signals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON damage_signals
    USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
    WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid);
