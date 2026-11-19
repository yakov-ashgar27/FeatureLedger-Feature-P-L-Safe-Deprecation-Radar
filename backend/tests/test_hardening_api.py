"""Security hardening over HTTP against real Postgres: session revocation, security headers, rate limits.

    FL_INTEGRATION=1 SANDBOX_MODE=true DATABASE_URL=... ADMIN_DATABASE_URL=... pytest tests/test_hardening_api.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import uuid

import pytest

if os.environ.get("FL_INTEGRATION") != "1":
    pytest.skip("integration suite: set FL_INTEGRATION=1 with a migrated, seeded database", allow_module_level=True)

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.core.ratelimit as rl  # noqa: E402
import app.main as main  # noqa: E402
from app.config import config  # noqa: E402


def admin_sql(sql: str, *args):
    async def go():
        conn = await asyncpg.connect(config.admin_database_url)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def probe_user():
    """A throwaway viewer in the seeded tenant, so revoking its sessions disturbs no other test."""
    email = f"probe-{uuid.uuid4().hex[:8]}@hardening.example"
    admin_sql("""INSERT INTO users (org_id, email, role) SELECT id, $1, 'viewer' FROM organizations WHERE slug = $2""",
              email, config.default_org_slug)
    yield email
    admin_sql("DELETE FROM users WHERE email = $1", email)


def login(client, email: str) -> dict:
    r = client.post("/api/v1/auth/dev-login", json={"email": email})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_logout_ends_every_session_of_that_user(client, probe_user):
    a, b = login(client, probe_user)["token"], None
    import time; time.sleep(1.1)                       # a second token with a different iat
    b = login(client, probe_user)["token"]
    assert a != b and client.get("/api/v1/auth/me", headers=H(a)).status_code == 200
    out = client.post("/api/v1/auth/logout", headers=H(a))
    assert out.status_code == 200 and out.json()["sessions_revoked"] is True
    for t in (a, b):                                   # the other browser / a copied token dies too
        r = client.get("/api/v1/auth/me", headers=H(t))
        assert r.status_code == 401 and r.json()["detail"]["code"] == "session_revoked"
    c = login(client, probe_user)["token"]             # signing in again works
    assert client.get("/api/v1/auth/me", headers=H(c)).status_code == 200


def test_role_change_disable_and_admin_revoke_end_sessions(client, probe_user):
    admin = login(client, admin_sql("SELECT u.email FROM users u JOIN organizations o ON o.id = u.org_id "
                                    "WHERE o.slug = $1 AND u.role = 'admin' AND NOT u.disabled ORDER BY u.created_at LIMIT 1",
                                    config.default_org_slug)[0]["email"])["token"]
    who = login(client, probe_user)
    uid, t = who["user"]["id"], who["token"]
    assert client.patch(f"/api/v1/users/{uid}", json={"role": "cs_lead"}, headers=H(admin)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=H(t)).status_code == 401
    t = login(client, probe_user)["token"]
    assert client.get("/api/v1/auth/me", headers=H(t)).json()["role"] == "cs_lead"
    assert client.post(f"/api/v1/users/{uid}/revoke-sessions", headers=H(admin)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=H(t)).status_code == 401
    t = login(client, probe_user)["token"]
    assert client.post(f"/api/v1/users/{uid}/revoke-sessions", headers=H(t)).status_code == 403   # admins only
    assert client.patch(f"/api/v1/users/{uid}", json={"disabled": True}, headers=H(admin)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=H(t)).status_code == 401
    assert client.post("/api/v1/auth/dev-login", json={"email": probe_user}).status_code == 404


def test_cookie_logout_without_csrf_header_only_clears_the_cookie(client, probe_user):
    t = login(client, probe_user)["token"]
    r = client.post("/api/v1/auth/logout", cookies={"fl_session": t})   # a cross-site form cannot add X-FL-CSRF
    assert r.status_code == 200 and r.json()["sessions_revoked"] is False
    assert client.get("/api/v1/auth/me", headers=H(t)).status_code == 200


def test_security_headers(client, probe_user):
    t = login(client, probe_user)["token"]
    for path in ("/api/v1/auth/me", "/api/v1/pnl", "/health"):
        h = client.get(path, headers=H(t)).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer" and "frame-ancestors 'none'" in h["content-security-policy"]
    assert client.get("/api/v1/pnl", headers=H(t)).headers["cache-control"] == "no-store"
    assert "content-security-policy" not in client.get("/docs").headers      # Swagger UI loads its own assets


def test_login_is_rate_limited_per_client_address(client, monkeypatch):
    monkeypatch.setattr(main, "config", dataclasses.replace(config, trusted_proxy_hops=1))
    monkeypatch.setattr(rl, "_limiter", rl.RateLimiter(None))                 # fresh counters for this test only
    attacker = {"X-Forwarded-For": "203.0.113.7"}
    codes = [client.post("/api/v1/auth/dev-login", json={"email": "nobody@nowhere.example"}, headers=attacker).status_code
             for _ in range(rl.LOGIN.limit + 1)]
    assert codes[:-1] == [404] * rl.LOGIN.limit and codes[-1] == 429
    r = client.post("/api/v1/auth/dev-login", json={"role": "viewer"}, headers=attacker)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["detail"]["code"] == "rate_limited"
    assert r.headers["x-content-type-options"] == "nosniff"
    other = client.post("/api/v1/auth/dev-login", json={"role": "viewer"}, headers={"X-Forwarded-For": "198.51.100.2"})
    assert other.status_code == 200                                            # other people are unaffected
