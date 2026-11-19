"""Sign-in endpoints (OIDC SSO + SANDBOX_MODE dev login), the caller's identity, and user admin."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..auth.oidc import PRESETS, OIDCClient, OIDCError, OIDCSettings, httpx_client, parse_role_map, role_from_claims
from ..auth.rbac import Role, matrix
from ..auth.session import (COOKIE, Principal, current_principal, issue_session, principal_from_user, require,
                            session_secret)
from ..auth.tokens import TokenError, sign_hs256, verify_hs256
from ..config import config
from ..db import auth_call, org_tx
from .deps import row

log = logging.getLogger("featureledger.auth")
router = APIRouter(prefix="/api/v1", tags=["auth"])
STATE_COOKIE = "fl_oidc"
_client: OIDCClient | None = None


def oidc_settings() -> OIDCSettings:
    preset = PRESETS.get(config.oidc_provider or "generic", PRESETS["generic"])
    issuer = config.oidc_issuer or preset.get("issuer", "")
    return OIDCSettings(
        provider=config.oidc_provider or "generic", issuer=issuer, client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret, redirect_uri=f"{config.app_url}/api/v1/auth/oidc/callback",
        scopes=config.oidc_scopes or preset["scopes"],
        role_claim=preset["role_claim"] if config.oidc_role_claim is None else config.oidc_role_claim,
        role_map=parse_role_map(config.oidc_role_map), default_role=Role(config.oidc_default_role),
        allowed_domains=config.oidc_allowed_domains)


def oidc_client() -> OIDCClient:
    global _client
    if _client is None:
        _client = httpx_client(oidc_settings())
    return _client


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def _set_session(resp: Response, token: str) -> None:
    resp.set_cookie(COOKIE, token, max_age=config.session_ttl_hours * 3600, httponly=True, samesite="lax",
                    secure=config.cookie_secure, path="/")


@router.get("/auth/config")
async def auth_config():
    s = oidc_settings() if config.oidc_enabled else None
    return {"sandbox_mode": config.sandbox_mode, "dev_login": config.dev_login_enabled, "demo_mode": config.demo_mode,
            "oidc": {"enabled": True, "provider": s.provider, "label": s.label} if s else {"enabled": False},
            "roles": [r.value for r in Role]}


class DevLogin(BaseModel):
    role: Literal["viewer", "pm_tech_lead", "cs_lead", "admin"] | None = None
    email: str | None = Field(default=None, max_length=320)


@router.post("/auth/dev-login")
async def dev_login(body: DevLogin, response: Response):
    """SANDBOX_MODE only: sign in as the seeded user of a role (or by email) without an IdP."""
    if not config.dev_login_enabled:
        raise HTTPException(404, "dev login is only available with SANDBOX_MODE=true")
    if not body.role and not body.email:
        raise HTTPException(422, "pass a role or an email")
    user = await auth_call("auth_dev_user", config.default_org_slug, body.role, body.email)
    if not user:
        raise HTTPException(404, f"no enabled seeded user for {body.role or body.email}")
    token = issue_session(user, "dev")
    _set_session(response, token)
    return {"token": token, "user": principal_from_user(user).public()}


@router.get("/auth/oidc/login")
async def oidc_login(next: str | None = None):
    if not config.oidc_enabled:
        raise HTTPException(404, "SSO is not configured (set OIDC_PROVIDER, OIDC_ISSUER, OIDC_CLIENT_ID)")
    try:
        url, st = await oidc_client().login_redirect()
    except Exception as e:  # discovery unreachable / malformed
        log.exception("OIDC discovery failed")
        raise HTTPException(502, f"identity provider unavailable: {e}") from e
    resp = RedirectResponse(url, status_code=302)
    cookie = sign_hs256({"typ": "oidc_state", **st, "next": _safe_next(next)}, session_secret(), 600)
    resp.set_cookie(STATE_COOKIE, cookie, max_age=600, httponly=True, samesite="lax", secure=config.cookie_secure,
                    path="/api/v1/auth/oidc")
    return resp


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str | None = None, state: str | None = None,
                        error: str | None = None, error_description: str | None = None):
    def fail(msg: str) -> RedirectResponse:
        from urllib.parse import quote
        r = RedirectResponse(f"/login?error={quote(msg[:200])}", status_code=302)
        r.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oidc")
        return r

    if error:
        return fail(error_description or error)
    try:
        st = verify_hs256(request.cookies.get(STATE_COOKIE, ""), session_secret(), typ="oidc_state")
    except TokenError:
        return fail("sign-in session expired; try again")
    if not code or not state or state != st.get("state"):
        return fail("state mismatch")
    s = oidc_settings()
    try:
        claims = await oidc_client().complete(code, st["verifier"], st["nonce"])
        role, authoritative = role_from_claims(claims, s)
        if claims["email"].lower() in config.bootstrap_admins:
            role, authoritative = Role.ADMIN, True
    except (OIDCError, TokenError, ValueError) as e:
        log.warning("OIDC login rejected: %s", e)
        return fail(str(e))
    user = await auth_call("auth_upsert_user", config.default_org_slug, claims["email"].lower(),
                           claims.get("name"), claims["iss"], str(claims["sub"]), role.value, authoritative)
    if not user or user["disabled"]:
        return fail("this account is disabled in FeatureLedger")
    resp = RedirectResponse(_safe_next(st.get("next")), status_code=302)
    _set_session(resp, issue_session(user, f"oidc:{s.provider}"))
    resp.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oidc")
    return resp


@router.get("/auth/me")
async def me(p: Principal = Depends(current_principal)):
    return p.public()


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/rbac")
async def rbac_matrix(p: Principal = Depends(current_principal)):
    return {"roles": [r.value for r in Role], "matrix": matrix(), "you": p.role.value}


# ------------------------------------------------------------------------------------------ users
@router.get("/users")
async def list_users(p: Principal = Depends(require("users:manage"))):
    async with org_tx(p.org_id) as conn:
        rows = await conn.fetch("""SELECT id, email, name, role::text AS role, disabled, last_login_at, created_at,
                                          oidc_subject IS NOT NULL AS sso FROM users ORDER BY email""")
    return [row(r) for r in rows]


class UserPatch(BaseModel):
    role: Literal["viewer", "pm_tech_lead", "cs_lead", "admin"] | None = None
    disabled: bool | None = None


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserPatch, p: Principal = Depends(require("users:manage"))):
    if p.is_demo:
        raise HTTPException(409, "users are fixed in the live demo (switch roles from the demo banner)")
    if user_id == p.user_id and (body.disabled or (body.role and body.role != "admin")):
        raise HTTPException(409, "you cannot demote or disable yourself (ask another admin)")
    async with org_tx(p.org_id) as conn:
        r = await conn.fetchrow("""UPDATE users SET role = coalesce($2::user_role, role),
                                                    disabled = coalesce($3, disabled)
                                    WHERE id = $1::uuid
                                RETURNING id, email, name, role::text AS role, disabled""",
                                user_id, body.role, body.disabled)
        if not r:
            raise HTTPException(404, "user not found")
    return row(r)
