"""Compact JWS helpers. Sessions are HS256 JWTs signed with SESSION_SECRET; OIDC id_tokens are
verified RS256 (or ES256) against the IdP's JWKS with the `cryptography` package. No other JWT lib."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


class TokenError(ValueError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def sign_hs256(claims: dict[str, Any], secret: str, ttl_seconds: int, now: float | None = None) -> str:
    now = int(now if now is not None else time.time())
    body = {**claims, "iat": now, "exp": now + ttl_seconds, "jti": secrets.token_urlsafe(8)}
    head = b64url(_json({"alg": "HS256", "typ": "JWT"}))
    payload = b64url(_json(body))
    sig = hmac.new(secret.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    return f"{head}.{payload}.{b64url(sig)}"


def split(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    try:
        h, p, s = token.split(".")
        header, claims = json.loads(b64url_decode(h)), json.loads(b64url_decode(p))
        return header, claims, f"{h}.{p}".encode(), b64url_decode(s)
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenError("malformed token") from e


def verify_hs256(token: str, secret: str, now: float | None = None, typ: str | None = None) -> dict[str, Any]:
    header, claims, signing_input, sig = split(token)
    if header.get("alg") != "HS256":           # never accept 'none' or an algorithm swap
        raise TokenError("unexpected algorithm")
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise TokenError("bad signature")
    if int(claims.get("exp", 0)) < int(now if now is not None else time.time()):
        raise TokenError("expired")
    if typ is not None and claims.get("typ") != typ:
        raise TokenError("wrong token type")
    return claims


# ------------------------------------------------------------------------------------- JWKS (OIDC)
def _int(b64: str) -> int:
    return int.from_bytes(b64url_decode(b64), "big")


def public_key_from_jwk(jwk: dict[str, Any]):
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    if jwk.get("kty") == "RSA":
        return rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
    if jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
        return ec.EllipticCurvePublicNumbers(_int(jwk["x"]), _int(jwk["y"]), ec.SECP256R1()).public_key()
    raise TokenError(f"unsupported JWK kty={jwk.get('kty')} crv={jwk.get('crv')}")


def verify_jws(token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Verify an RS256/ES256 signature against a JWKS document; returns the (unvalidated) claims."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    header, claims, signing_input, sig = split(token)
    alg = header.get("alg")
    if alg not in ("RS256", "ES256"):
        raise TokenError(f"unsupported id_token alg {alg!r}")
    keys = [k for k in jwks.get("keys", []) if k.get("use", "sig") == "sig"
            and (header.get("kid") is None or k.get("kid") == header.get("kid"))]
    if not keys:
        raise TokenError("no matching signing key in JWKS")
    for jwk in keys:
        try:
            key = public_key_from_jwk(jwk)
            if alg == "RS256":
                key.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
            else:
                if len(sig) != 64:
                    raise TokenError("bad ES256 signature length")
                der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
                key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
            return claims
        except (InvalidSignature, TokenError, ValueError, TypeError):
            continue
    raise TokenError("id_token signature did not verify")
