"""Enterprise SSO: OpenID Connect authorization-code flow with PKCE (Okta, Google Workspace, Auth0,
or any compliant IdP).

    /auth/oidc/login     -> state + nonce + PKCE verifier in a signed 10-minute cookie, 302 to the IdP
    /auth/oidc/callback  -> check state, exchange code (+ verifier), verify id_token (RS256/ES256 via
                            JWKS; iss, aud/azp, exp, iat, nonce), map claims -> role, upsert the user
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

from .rbac import Role
from .tokens import TokenError, b64url, verify_jws

LEEWAY = 60
ROLE_PRIORITY = [Role.ADMIN, Role.PM_TECH_LEAD, Role.CS_LEAD, Role.VIEWER]

# Provider presets: sensible defaults; everything can be overridden with OIDC_* variables.
PRESETS: dict[str, dict[str, str]] = {
    "okta":   {"label": "Okta", "role_claim": "groups", "scopes": "openid email profile groups"},
    "google": {"label": "Google Workspace", "issuer": "https://accounts.google.com", "role_claim": "",
               "scopes": "openid email profile"},
    "auth0":  {"label": "Auth0", "role_claim": "https://featureledger.app/roles", "scopes": "openid email profile"},
    "generic": {"label": "SSO", "role_claim": "", "scopes": "openid email profile"},
}

HttpGet = Callable[[str], Awaitable[dict[str, Any]]]
HttpPostForm = Callable[[str, dict[str, str], tuple[str, str] | None], Awaitable[dict[str, Any]]]


class OIDCError(ValueError):
    pass


@dataclass(frozen=True)
class OIDCSettings:
    provider: str
    issuer: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    scopes: str
    role_claim: str = ""
    role_map: dict[str, Role] = field(default_factory=dict)
    default_role: Role = Role.VIEWER
    allowed_domains: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return PRESETS.get(self.provider, PRESETS["generic"])["label"]


def parse_role_map(raw: str) -> dict[str, Role]:
    """'FL-Admins=admin, FL-PM=pm_tech_lead' -> {'FL-Admins': Role.ADMIN, ...}"""
    out: dict[str, Role] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        k, sep, v = part.rpartition("=") if "=" in part else part.rpartition(":")
        if not sep:
            raise OIDCError(f"OIDC_ROLE_MAP entry {part!r} must look like group=role")
        out[k.strip()] = Role(v.strip())
    return out


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorization_url(disc: dict[str, Any], s: OIDCSettings, state: str, nonce: str, challenge: str) -> str:
    params = {"response_type": "code", "client_id": s.client_id, "redirect_uri": s.redirect_uri, "scope": s.scopes,
              "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    if s.provider == "google" and len(s.allowed_domains) == 1:
        params["hd"] = s.allowed_domains[0]   # hint only; the hd claim is verified on the way back
    return disc["authorization_endpoint"] + "?" + urlencode(params)


def validate_claims(claims: dict[str, Any], s: OIDCSettings, issuer: str, nonce: str,
                    now: float | None = None) -> None:
    now = now if now is not None else time.time()
    if claims.get("iss") != issuer:
        raise OIDCError("id_token issuer mismatch")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if s.client_id not in auds:
        raise OIDCError("id_token audience mismatch")
    if len(auds) > 1 and claims.get("azp") != s.client_id:
        raise OIDCError("id_token azp mismatch")
    if float(claims.get("exp", 0)) < now - LEEWAY:
        raise OIDCError("id_token expired")
    if float(claims.get("iat", now)) > now + LEEWAY:
        raise OIDCError("id_token issued in the future")
    if not nonce or claims.get("nonce") != nonce:
        raise OIDCError("id_token nonce mismatch (possible replay)")
    email = (claims.get("email") or "").lower()
    if not email:
        raise OIDCError("id_token has no email claim; add the 'email' scope")
    if claims.get("email_verified") is False:
        raise OIDCError("email is not verified by the identity provider")
    if s.allowed_domains:
        domain = email.rsplit("@", 1)[-1]
        hd = claims.get("hd")
        if domain not in s.allowed_domains and hd not in s.allowed_domains:
            raise OIDCError(f"{domain} is not an allowed sign-in domain")


def role_from_claims(claims: dict[str, Any], s: OIDCSettings) -> tuple[Role, bool]:
    """-> (role, idp_is_authoritative). With a role claim configured the IdP owns roles (group changes
    in Okta/Auth0 take effect at next login); without one, admins assign roles in FeatureLedger."""
    if not s.role_claim:
        return s.default_role, False
    raw = claims.get(s.role_claim)
    values = raw if isinstance(raw, list) else [raw] if raw else []
    matched = {s.role_map[v] for v in values if v in s.role_map}
    matched |= {Role(v) for v in values if v in Role._value2member_map_}
    for r in ROLE_PRIORITY:
        if r in matched:
            return r, True
    return s.default_role, True


@dataclass
class OIDCClient:
    settings: OIDCSettings
    http_get: HttpGet
    http_post_form: HttpPostForm
    _disc: dict[str, Any] | None = None
    _jwks: dict[str, Any] | None = None
    _jwks_at: float = 0.0

    async def discovery(self) -> dict[str, Any]:
        if self._disc is None:
            d = await self.http_get(self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration")
            for k in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
                if k not in d:
                    raise OIDCError(f"discovery document lacks {k}")
            self._disc = d
        return self._disc

    async def jwks(self, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._jwks is None or time.time() - self._jwks_at > 3600:
            self._jwks = await self.http_get((await self.discovery())["jwks_uri"])
            self._jwks_at = time.time()
        return self._jwks

    async def login_redirect(self) -> tuple[str, dict[str, str]]:
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        url = authorization_url(await self.discovery(), self.settings, state, nonce, challenge)
        return url, {"state": state, "nonce": nonce, "verifier": verifier}

    async def complete(self, code: str, verifier: str, nonce: str) -> dict[str, Any]:
        disc = await self.discovery()
        s = self.settings
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": s.redirect_uri,
                "client_id": s.client_id, "code_verifier": verifier}
        tok = await self.http_post_form(disc["token_endpoint"], form,
                                        (s.client_id, s.client_secret) if s.client_secret else None)
        id_token = tok.get("id_token")
        if not id_token:
            raise OIDCError(f"token endpoint returned no id_token ({tok.get('error', 'unknown error')})")
        try:
            claims = verify_jws(id_token, await self.jwks())
        except TokenError:
            claims = verify_jws(id_token, await self.jwks(refresh=True))   # key rotation
        validate_claims(claims, s, disc["issuer"], nonce)
        return claims


def httpx_client(settings: OIDCSettings) -> OIDCClient:
    async def get(url: str) -> dict[str, Any]:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()

    async def post_form(url: str, data: dict[str, str], auth: tuple[str, str] | None) -> dict[str, Any]:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, data=data, auth=auth, headers={"Accept": "application/json"})
            return r.json()

    return OIDCClient(settings, get, post_form)
