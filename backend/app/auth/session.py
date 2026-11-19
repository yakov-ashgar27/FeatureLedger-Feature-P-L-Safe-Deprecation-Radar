"""Who is calling: session token (cookie or bearer) -> Principal; FastAPI dependencies that enforce RBAC.

    fl_session cookie   browser sessions (HttpOnly, SameSite=Lax). Unsafe methods must also send
                        `X-FL-CSRF: 1`, a header a cross-site form cannot set.
    Authorization: Bearer <session jwt>   scripts, CI, the Next.js server forwarding the cookie
    Authorization: Bearer <FL_API_TOKEN>  machine credential = admin service principal (no user)

The tenant always comes from the user's record in the database, never from a header the caller sends.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import config
from ..db import auth_call, resolve_org
from .rbac import PERMISSIONS, Role, can, normalize_role, permissions_for
from .tokens import TokenError, sign_hs256, verify_hs256

log = logging.getLogger("featureledger.auth")
COOKIE = "fl_session"
CSRF_HEADER = "x-fl-csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SANDBOX_SECRET = "featureledger-sandbox-only-session-secret"
_ephemeral: str | None = None


def session_secret() -> str:
    global _ephemeral
    if config.session_secret:
        return config.session_secret
    if config.sandbox_mode:
        return _SANDBOX_SECRET
    if _ephemeral is None:
        log.warning("SESSION_SECRET is not set: using a random per-process key (sessions end on restart)")
        _ephemeral = secrets.token_urlsafe(48)
    return _ephemeral


@dataclass(frozen=True)
class Principal:
    org_id: str
    org_slug: str
    email: str
    role: Role
    user_id: str | None = None
    name: str | None = None
    kind: str = "user"          # user | service

    def can(self, permission: str) -> bool:
        return can(self.role, permission)

    @property
    def is_demo(self) -> bool:
        return self.org_slug == config.demo_org_slug and config.demo_mode

    def public(self) -> dict[str, Any]:
        return {"id": self.user_id, "email": self.email, "name": self.name, "role": self.role.value,
                "kind": self.kind, "org": {"id": self.org_id, "slug": self.org_slug}, "demo": self.is_demo,
                "permissions": permissions_for(self.role)}

    def audit(self) -> dict[str, Any]:
        return {"actor": self.email, "actor_role": self.role.value, "actor_kind": self.kind}


def issue_session(user: dict[str, Any], method: str) -> str:
    return sign_hs256({"typ": "session", "sub": str(user["id"]), "org": str(user["org_id"]), "amr": method},
                      session_secret(), config.session_ttl_hours * 3600)


def principal_from_user(u: dict[str, Any]) -> Principal:
    return Principal(org_id=str(u["org_id"]), org_slug=u["org_slug"], email=u["email"], name=u.get("name"),
                     role=normalize_role(u["role"]), user_id=str(u["id"]))


_service_orgs: dict[str, str] = {}


async def _service_principal(slug: str) -> Principal:
    if slug not in _service_orgs:
        org = await resolve_org(slug)
        if not org:
            raise HTTPException(404, f"unknown organization {slug!r}")
        _service_orgs[slug] = org
    return Principal(org_id=_service_orgs[slug], org_slug=slug, email="service@featureledger", role=Role.ADMIN,
                     kind="service")


# shows the "Authorize" button in /docs; paste a token from POST /api/v1/auth/dev-login (or FL_API_TOKEN)
bearer_scheme = HTTPBearer(auto_error=False, description="Session token (dev login / SSO) or FL_API_TOKEN")


async def current_principal(request: Request,
                            creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                            x_org_slug: str | None = Header(default=None)) -> Principal:
    bearer = creds.credentials.strip() if creds else ""
    token, via_cookie = (bearer, False) if bearer else (request.cookies.get(COOKIE, ""), True)
    if not token:
        raise HTTPException(401, {"code": "unauthenticated", "message": "sign in first"})
    if config.api_token and hmac.compare_digest(token.encode(), config.api_token.encode()):
        return await _service_principal(x_org_slug or config.default_org_slug)
    try:
        claims = verify_hs256(token, session_secret(), typ="session")
    except TokenError as e:
        raise HTTPException(401, {"code": "invalid_session", "message": str(e)}) from e
    if via_cookie and request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, {"code": "csrf", "message": f"cookie-authenticated writes need {CSRF_HEADER}: 1"})
    user = await auth_call("auth_get_user", claims["sub"])
    if not user or user["disabled"] or str(user["org_id"]) != claims.get("org"):
        raise HTTPException(401, {"code": "invalid_session", "message": "user no longer active"})
    return principal_from_user(user)


def forbidden(p: Principal, permission: str) -> HTTPException:
    return HTTPException(403, {"code": "forbidden", "permission": permission, "role": p.role.value,
                               "message": f"role {p.role.value} may not {permission}"})


def require(permission: str) -> Callable[..., Any]:
    if permission not in PERMISSIONS:
        raise KeyError(permission)

    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(permission):
            raise forbidden(p, permission)
        return p
    dep.__name__ = f"require_{permission.replace(':', '_')}"
    return dep


def check(p: Principal, permission: str) -> None:
    if not p.can(permission):
        raise forbidden(p, permission)


require_read = require("read")
