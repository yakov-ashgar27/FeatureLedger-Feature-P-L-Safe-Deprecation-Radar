"""Phase 4 auth: RBAC matrix, session tokens, OIDC id_token verification (RS256/ES256 via JWKS)."""
import asyncio
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.auth.oidc import (OIDCClient, OIDCError, OIDCSettings, authorization_url, parse_role_map, pkce_pair,
                           role_from_claims, validate_claims)
from app.auth.rbac import PERMISSIONS, Role, can, normalize_role, permissions_for
from app.auth.tokens import TokenError, b64url, sign_hs256, verify_hs256, verify_jws

V, P, C, A = "viewer", "pm_tech_lead", "cs_lead", "admin"

# The contract from the Phase 4 brief, written out independently of rbac.py.
EXPECTED = {
    "read": {V, P, C, A},
    "triage:decide": {P, A},
    "graph:run": {P, A},
    "pnl:recompute": {P, A},
    "sunset:create": {P, A},
    "sunset:blast_radius": {P, A},
    "sunset:dossier": {P, C, A},
    "sunset:cs_signoff": {C, A},
    "sunset:approve_canary": {A},
    "sunset:approve_cleanup": {A},
    "sunset:rollback": {P, C, A},
    "sunset:cancel": {P, A},
    "integrations:manage": {A},
    "manifest:sync": {A},
    "imports:write": {A},
    "settings:write": {A},
    "users:manage": {A},
    "flags:simulate": {A},
    "reports:send": {P, A},
}


def test_rbac_matrix_matches_the_contract_for_all_four_roles():
    assert set(PERMISSIONS) == set(EXPECTED)
    for perm, allowed in EXPECTED.items():
        for role in (V, P, C, A):
            assert can(role, perm) == (role in allowed), (role, perm)


def test_viewer_is_read_only_and_admin_has_everything():
    assert permissions_for(V) == ["read"]
    assert set(permissions_for(A)) == set(EXPECTED)
    # the separation of duties the brief asks for
    assert not can(P, "sunset:cs_signoff") and not can(C, "sunset:create")
    assert not can(P, "sunset:approve_canary") and not can(C, "sunset:approve_cleanup")


def test_legacy_and_unknown_roles_are_mapped_conservatively():
    assert normalize_role("pm") is Role.PM_TECH_LEAD and normalize_role("eng_lead") is Role.PM_TECH_LEAD
    assert normalize_role("cs") is Role.CS_LEAD and normalize_role("finance") is Role.VIEWER
    assert normalize_role("superuser") is Role.VIEWER and normalize_role(None) is Role.VIEWER
    with pytest.raises(KeyError):
        can(A, "no:such:permission")


# ----------------------------------------------------------------------------------------- sessions
def test_session_token_roundtrip_and_tampering():
    tok = sign_hs256({"typ": "session", "sub": "u1", "org": "o1"}, "s3cret", 60)
    assert verify_hs256(tok, "s3cret", typ="session")["sub"] == "u1"
    with pytest.raises(TokenError):
        verify_hs256(tok, "other-secret")
    h, p, s = tok.split(".")
    forged = b64url(json.dumps({"typ": "session", "sub": "admin", "org": "o1", "exp": 9999999999}).encode())
    with pytest.raises(TokenError):
        verify_hs256(f"{h}.{forged}.{s}", "s3cret")
    with pytest.raises(TokenError, match="type"):
        verify_hs256(tok, "s3cret", typ="oidc_state")
    with pytest.raises(TokenError, match="expired"):
        verify_hs256(sign_hs256({"typ": "session"}, "s3cret", 60, now=time.time() - 3600), "s3cret")


def test_alg_none_is_rejected():
    head = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64url(json.dumps({"typ": "session", "sub": "x", "exp": 9999999999}).encode())
    with pytest.raises(TokenError, match="algorithm"):
        verify_hs256(f"{head}.{body}.", "s3cret")
    with pytest.raises(TokenError):
        verify_hs256("not-a-jwt", "s3cret")


# ----------------------------------------------------------------------------------------- OIDC
ISS = "https://example.okta.com/oauth2/default"


def _rsa():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = key.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256",
           "n": b64url(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")), "e": b64url(n.e.to_bytes(3, "big"))}
    return key, jwk


def _sign_rs256(key, claims, kid="k1"):
    h = b64url(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    p = b64url(json.dumps(claims).encode())
    sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{b64url(sig)}"


def _claims(**kw):
    now = int(time.time())
    c = {"iss": ISS, "aud": "client-1", "sub": "00u1", "email": "Pat@Northwind.io", "email_verified": True,
         "name": "Pat", "nonce": "n-1", "iat": now, "exp": now + 300, "groups": ["FL-Product", "Everyone"]}
    c.update(kw)
    return c


def _settings(**kw):
    base = dict(provider="okta", issuer=ISS, client_id="client-1", client_secret="sec",
                redirect_uri="http://localhost:3000/api/v1/auth/oidc/callback", scopes="openid email profile groups",
                role_claim="groups", role_map=parse_role_map("FL-Admins=admin,FL-Product=pm_tech_lead,FL-CS=cs_lead"))
    base.update(kw)
    return OIDCSettings(**base)


def test_pkce_and_authorization_url():
    verifier, challenge = pkce_pair()
    import base64, hashlib
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = authorization_url({"authorization_endpoint": f"{ISS}/v1/authorize"}, _settings(), "st", "no", challenge)
    assert "code_challenge_method=S256" in url and "state=st" in url and "nonce=no" in url
    assert "response_type=code" in url and "client_id=client-1" in url


def test_full_code_exchange_verifies_signature_and_claims():
    key, jwk = _rsa()
    seen = {}

    async def get(url):
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISS, "authorization_endpoint": f"{ISS}/v1/authorize", "token_endpoint": f"{ISS}/v1/token",
                    "jwks_uri": f"{ISS}/v1/keys"}
        return {"keys": [jwk]}

    async def post(url, data, auth):
        seen.update(data=data, auth=auth)
        return {"id_token": _sign_rs256(key, _claims())}

    client = OIDCClient(_settings(), get, post)
    claims = asyncio.run(client.complete("the-code", "the-verifier", "n-1"))
    assert claims["email"] == "Pat@Northwind.io"
    assert seen["data"]["code_verifier"] == "the-verifier" and seen["data"]["grant_type"] == "authorization_code"
    assert seen["auth"] == ("client-1", "sec")
    assert role_from_claims(claims, _settings()) == (Role.PM_TECH_LEAD, True)


def test_wrong_key_wrong_nonce_wrong_audience_expired_are_rejected():
    key, jwk = _rsa()
    other, _ = _rsa()
    jwks = {"keys": [jwk]}
    with pytest.raises(TokenError):
        verify_jws(_sign_rs256(other, _claims()), jwks)
    good = verify_jws(_sign_rs256(key, _claims()), jwks)
    validate_claims(good, _settings(), ISS, "n-1")
    for bad, msg in [(_claims(nonce="replayed"), "nonce"), (_claims(aud="someone-else"), "audience"),
                     (_claims(iss="https://evil.example"), "issuer"), (_claims(exp=int(time.time()) - 3600), "expired"),
                     (_claims(email_verified=False), "verified"), (_claims(aud=["client-1", "x"]), "azp")]:
        with pytest.raises(OIDCError, match=msg):
            validate_claims(bad, _settings(), ISS, "n-1")


def test_es256_keys_are_supported():
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "kid": "e1", "x": b64url(nums.x.to_bytes(32, "big")),
           "y": b64url(nums.y.to_bytes(32, "big"))}
    h = b64url(json.dumps({"alg": "ES256", "kid": "e1"}).encode())
    p = b64url(json.dumps(_claims()).encode())
    r, s = decode_dss_signature(key.sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256())))
    tok = f"{h}.{p}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    assert verify_jws(tok, {"keys": [jwk]})["sub"] == "00u1"


def test_google_workspace_domain_allowlist_and_roles_without_claim():
    g = _settings(provider="google", issuer="https://accounts.google.com", role_claim="", allowed_domains=("northwind.io",))
    validate_claims(_claims(iss="https://accounts.google.com", email="a@northwind.io"), g,
                    "https://accounts.google.com", "n-1")
    with pytest.raises(OIDCError, match="allowed"):
        validate_claims(_claims(iss="https://accounts.google.com", email="a@gmail.com"), g,
                        "https://accounts.google.com", "n-1")
    assert role_from_claims(_claims(), g) == (Role.VIEWER, False)   # roles are managed in FeatureLedger


def test_role_mapping_picks_the_most_privileged_match():
    s = _settings(role_claim="https://featureledger.app/roles")
    assert role_from_claims({"https://featureledger.app/roles": ["FL-CS", "FL-Admins"]}, s) == (Role.ADMIN, True)
    assert role_from_claims({"https://featureledger.app/roles": "cs_lead"}, s) == (Role.CS_LEAD, True)
    assert role_from_claims({}, s) == (Role.VIEWER, True)
    with pytest.raises(OIDCError):
        parse_role_map("no-equals-sign")
