-- Session revocation. A session token carries the user's session_epoch at sign-in; every request
-- compares it with the stored value. Bumping the epoch ends every session that user has, at once:
-- on sign-out, on "sign out everywhere", and whenever an admin disables the user or changes the role.
-- (Role and disabled are also re-read on every request, so this mainly kills copied or stolen tokens.)

ALTER TABLE users ADD COLUMN session_epoch integer NOT NULL DEFAULT 0;

-- The three pre-tenant lookups gain session_epoch in their result, so they are recreated.
DROP FUNCTION auth_upsert_user(text, text, text, text, text, text, boolean);
DROP FUNCTION auth_dev_user(text, text, text);
DROP FUNCTION auth_get_user(uuid);

CREATE FUNCTION auth_get_user(p_user_id uuid)
RETURNS TABLE (id uuid, org_id uuid, org_slug text, email text, name text, role text, disabled boolean,
               session_epoch integer)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT u.id, u.org_id, o.slug, u.email, u.name, u.role::text, u.disabled, u.session_epoch
     FROM users u JOIN organizations o ON o.id = u.org_id WHERE u.id = p_user_id $$;

CREATE FUNCTION auth_dev_user(p_org_slug text, p_role text, p_email text)
RETURNS TABLE (id uuid, org_id uuid, org_slug text, email text, name text, role text, disabled boolean,
               session_epoch integer)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT u.id, u.org_id, o.slug, u.email, u.name, u.role::text, u.disabled, u.session_epoch
     FROM users u JOIN organizations o ON o.id = u.org_id
    WHERE o.slug = p_org_slug AND NOT u.disabled
      AND (p_email IS NULL OR lower(u.email) = lower(p_email))
      AND (p_role IS NULL OR u.role::text = p_role)
    ORDER BY u.created_at, u.email LIMIT 1 $$;

CREATE FUNCTION auth_upsert_user(p_org_slug text, p_email text, p_name text, p_issuer text, p_subject text,
                                 p_role text, p_idp_role boolean)
RETURNS TABLE (id uuid, org_id uuid, org_slug text, email text, name text, role text, disabled boolean,
               session_epoch integer)
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
        -- an IdP-driven role change ends the user's existing sessions too
        UPDATE users u SET name = coalesce(p_name, u.name), oidc_issuer = p_issuer, oidc_subject = p_subject,
                           last_login_at = now(),
                           session_epoch = u.session_epoch
                               + CASE WHEN p_idp_role AND u.role::text <> p_role THEN 1 ELSE 0 END,
                           role = CASE WHEN p_idp_role THEN p_role::user_role ELSE u.role END
         WHERE u.id = v_id;
    END IF;
    RETURN QUERY SELECT * FROM auth_get_user(v_id);
END $$;

REVOKE ALL ON FUNCTION auth_get_user(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_dev_user(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_upsert_user(text, text, text, text, text, text, boolean) FROM PUBLIC;
