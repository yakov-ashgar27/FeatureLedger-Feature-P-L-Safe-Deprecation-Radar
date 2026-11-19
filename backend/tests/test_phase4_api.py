"""Phase 4 end to end over HTTP against real Postgres (RLS on, app role), with the sandbox flag and
GitHub providers. CI runs it after `python -m app.migrate` (full seed):

    FL_INTEGRATION=1 SANDBOX_MODE=true DATABASE_URL=... ADMIN_DATABASE_URL=... pytest tests/test_phase4_api.py

Covers: authentication, RBAC boundaries for all four roles, tenant isolation, the scream-test 409 guards,
cleanup PR creation, the PR-merge webhook archiving the feature, and flag rollback.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

if os.environ.get("FL_INTEGRATION") != "1":
    pytest.skip("integration suite: set FL_INTEGRATION=1 with a migrated, seeded database", allow_module_level=True)

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth.rbac import permissions_for  # noqa: E402
from app.auth.session import issue_session  # noqa: E402
from app.config import config  # noqa: E402
from app.main import app  # noqa: E402

ROLES = ("viewer", "pm_tech_lead", "cs_lead", "admin")
NIL = "00000000-0000-0000-0000-000000000000"


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
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def tokens(client):
    out = {}
    for role in ROLES:
        r = client.post("/api/v1/auth/dev-login", json={"role": role})
        assert r.status_code == 200, r.text
        assert r.cookies.get("fl_session") or "fl_session" in r.headers.get("set-cookie", "")
        out[role] = r.json()["token"]
    client.cookies.clear()     # every request below authenticates explicitly
    return out


def H(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def plan_id(client, tokens, feature_key: str) -> str:
    plans = client.get("/api/v1/sunset-plans", headers=H(tokens["viewer"])).json()
    return next(p["id"] for p in plans if p["feature_key"] == feature_key and p["state"] not in ("archived", "cancelled"))


# ------------------------------------------------------------------------------------- authentication
def test_requests_without_a_valid_session_are_rejected(client, tokens):
    client.cookies.clear()
    assert client.get("/api/v1/pnl").status_code == 401
    assert client.get("/api/v1/pnl", headers=H("garbage")).status_code == 401
    t = tokens["viewer"]
    forged = t[:-4] + ("AAAA" if not t.endswith("AAAA") else "BBBB")
    assert client.get("/api/v1/pnl", headers=H(forged)).status_code == 401
    assert client.get("/api/v1/auth/config").status_code == 200          # public
    assert client.get("/health").status_code == 200


def test_dev_login_issues_role_scoped_sessions(client, tokens):
    for role in ROLES:
        me = client.get("/api/v1/auth/me", headers=H(tokens[role])).json()
        assert me["role"] == role and me["org"]["slug"] == "northwind"
        assert me["permissions"] == permissions_for(role)
    assert client.post("/api/v1/auth/dev-login", json={"role": "admin", "email": "nobody@nowhere.io"}).status_code == 404


def test_cookie_sessions_need_the_csrf_header_for_writes(client, tokens):
    client.cookies.clear()
    cookie = {"Cookie": f"fl_session={tokens['admin']}"}
    assert client.get("/api/v1/auth/me", headers=cookie).status_code == 200
    r = client.put("/api/v1/settings", json={"eng_hourly_rate_usd": 96}, headers=cookie)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf"
    r = client.put("/api/v1/settings?recompute_pnl=false", json={"eng_hourly_rate_usd": 95},
                   headers={**cookie, "X-FL-CSRF": "1"})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------------------------- RBAC
def boundary_cases(bulk: str):
    """(method, path, json, roles allowed). Allowed calls are built to stop after the auth check
    (404 / 409 / 422) so the matrix can be exercised without moving any plan."""
    return [
        ("GET", "/api/v1/pnl?window=3", None, set(ROLES)),
        ("GET", f"/api/v1/sunset-plans/{bulk}", None, set(ROLES)),
        ("POST", "/api/v1/simulate", {"feature_keys": ["legacy-pdf-export"]}, set(ROLES)),
        ("POST", f"/api/v1/triage/{NIL}", {"decision": "reject"}, {"pm_tech_lead", "admin"}),
        ("POST", "/api/v1/sunset-plans", {"feature_key": "no-such-feature", "reason": "boundary test only"},
         {"pm_tech_lead", "admin"}),
        ("POST", f"/api/v1/sunset-plans/{bulk}/evidence/blast_radius", None, {"pm_tech_lead", "admin"}),
        ("POST", f"/api/v1/sunset-plans/{bulk}/evidence/impact_dossier", None, {"pm_tech_lead", "cs_lead", "admin"}),
        ("POST", f"/api/v1/sunset-plans/{bulk}/acknowledge", {"kind": "account", "ids": ["*"]}, {"cs_lead", "admin"}),
        ("POST", f"/api/v1/sunset-plans/{bulk}/acknowledge", {"kind": "inbound", "ids": ["*"]}, {"pm_tech_lead", "admin"}),
        # bulk-csv sits in blast_radius_checked: the next step is the CS sign-off
        ("POST", f"/api/v1/sunset-plans/{bulk}/advance", {"expected_version": -1}, {"cs_lead", "admin"}),
        ("POST", f"/api/v1/sunset-plans/{bulk}/rollback", {"reason": "boundary"}, {"pm_tech_lead", "cs_lead", "admin"}),
        ("POST", f"/api/v1/sunset-plans/{NIL}/cancel", {"reason": "boundary"}, {"pm_tech_lead", "admin"}),
        ("POST", f"/api/v1/sunset-plans/{NIL}/cleanup-pr/simulate-merge", None, {"admin"}),
        ("POST", "/api/v1/flags/no-such-flag/evaluations", {"reset": True}, {"admin"}),
        ("PUT", "/api/v1/settings", {}, {"admin"}),
        ("GET", "/api/v1/users", None, {"admin"}),
        ("PATCH", f"/api/v1/users/{NIL}", {"role": "viewer"}, {"admin"}),
        ("POST", f"/api/v1/integrations/{NIL}/sync", None, {"admin"}),
        ("POST", "/api/v1/import/accounts?period=2026-10", None, {"admin"}),
        ("POST", "/api/v1/manifest/apply", None, {"admin"}),
    ]


def test_rbac_boundaries_for_all_four_roles(client, tokens):
    bulk = plan_id(client, tokens, "bulk-csv-import-v1")
    failures = []
    for method, path, body, allowed in boundary_cases(bulk):
        for role in ROLES:
            r = client.request(method, path, json=body, headers=H(tokens[role]))
            if role in allowed:
                ok = r.status_code not in (401, 403)
            else:
                ok = r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
            if not ok:
                failures.append(f"{role:13} {method} {path} -> {r.status_code} {r.text[:160]}")
    assert not failures, "\n".join(failures)


def test_advance_permission_depends_on_the_step(client, tokens):
    bulk = plan_id(client, tokens, "bulk-csv-import-v1")
    # a PM may not pass cs_signoff even where they could otherwise act
    r = client.post(f"/api/v1/sunset-plans/{bulk}/advance", json={"cs_signoff": True}, headers=H(tokens["pm_tech_lead"]))
    assert r.status_code == 403 and r.json()["detail"]["permission"] == "sunset:cs_signoff"


# ------------------------------------------------------------------------------------- tenant isolation
def test_tenant_isolation_with_a_second_org(client, tokens):
    org2 = admin_sql("INSERT INTO organizations (name, slug) VALUES ('Other Co', $1) RETURNING id",
                     f"other-{uuid.uuid4().hex[:8]}")[0]["id"]
    admin_sql("INSERT INTO org_settings (org_id) VALUES ($1)", org2)
    uid = admin_sql("""INSERT INTO users (org_id, email, name, role) VALUES ($1, 'root@other.example', 'Other Admin', 'admin')
                       RETURNING id""", org2)[0]["id"]
    other = issue_session({"id": uid, "org_id": org2}, "test")
    zap = plan_id(client, tokens, "zapier-v1-webhooks")

    assert client.get("/api/v1/sunset-plans", headers=H(other)).json() == []
    assert client.get(f"/api/v1/sunset-plans/{zap}", headers=H(other)).status_code == 404
    assert client.get("/api/v1/features/zapier-v1-webhooks", headers=H(other)).status_code == 404
    users = client.get("/api/v1/users", headers=H(other)).json()
    assert [u["email"] for u in users] == ["root@other.example"]
    # a header naming another tenant changes nothing for a user session
    hdr = {**H(other), "X-Org-Slug": "northwind"}
    assert client.get("/api/v1/sunset-plans", headers=hdr).json() == []
    assert client.get("/api/v1/flags", headers=hdr).json()["flags"] == []
    # cross-tenant writes find nothing to act on
    r = client.post(f"/api/v1/sunset-plans/{zap}/advance", json={}, headers=H(other))
    assert r.status_code == 404
    # a token whose org claim disagrees with the user's record is refused
    northwind = client.get("/api/v1/auth/me", headers=H(tokens["admin"])).json()["org"]["id"]
    forged = issue_session({"id": uid, "org_id": northwind}, "test")
    assert client.get("/api/v1/pnl", headers=H(forged)).status_code == 401
    # a disabled user loses access immediately
    admin_sql("UPDATE users SET disabled = true WHERE id = $1", uid)
    assert client.get("/api/v1/sunset-plans", headers=H(other)).status_code == 401


# ------------------------------------------------------------------------------------- scream test -> PR -> merge
def test_scream_test_guards_then_cleanup_pr_then_merge_archives(client, tokens):
    A, P = H(tokens["admin"]), H(tokens["pm_tech_lead"])
    zap = plan_id(client, tokens, "zapier-v1-webhooks")
    plan = client.get(f"/api/v1/sunset-plans/{zap}", headers=A).json()
    assert plan["state"] == "canary_dark_launch" and plan["canary_phase"] == "fully_off"
    assert plan["flag"]["state"] == "fully_off"
    assert plan["scream_test"]["quiet_days"] >= plan["scream_test"]["required_days"]

    r = client.post(f"/api/v1/sunset-plans/{zap}/advance", json={}, headers=P)
    assert r.status_code == 403 and r.json()["detail"]["permission"] == "sunset:approve_cleanup"

    # 1) the flag served 'on' during the scream test -> 409 flag_still_served
    assert client.post("/api/v1/flags/zapier-v1/evaluations", json={"on_count": 2, "off_count": 5}, headers=A).status_code == 200
    r = client.post(f"/api/v1/sunset-plans/{zap}/advance", json={}, headers=A)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "flag_still_served"
    audit = client.get(f"/api/v1/sunset-plans/{zap}", headers=A).json()["audit_log"]
    assert any("scream_alert" in a["payload"] for a in audit)        # the monitor noted it
    assert client.post("/api/v1/flags/zapier-v1/evaluations", json={"reset": True}, headers=A).json()["removed_buckets"] >= 1

    # 2) a customer ticket during the scream test -> 409 customer_screamed
    fid = admin_sql("""SELECT f.id, f.org_id FROM features f JOIN organizations o ON o.id = f.org_id
                     WHERE f.key = 'zapier-v1-webhooks' AND o.slug = 'northwind'""")[0]
    admin_sql("""INSERT INTO support_tickets (org_id, external_id, feature_id, mapping_source, created_at)
                 VALUES ($1, 'ci-scream-1', $2, 'human', now())""", fid["org_id"], fid["id"])
    r = client.post(f"/api/v1/sunset-plans/{zap}/advance", json={}, headers=A)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "customer_screamed"
    admin_sql("DELETE FROM support_tickets WHERE external_id = 'ci-scream-1' AND org_id = $1", fid["org_id"])

    # 2b) production damage during the scream test blocks the cleanup PR until an admin dismisses it
    hook = f"/api/v1/webhooks/signals/sentry/{config.default_org_slug}"
    sentry = {"action": "created", "data": {"error": {
        "event_id": "ci0001", "title": "KeyError: 'zap' for jane.doe@example.com", "tags": [["environment", "production"]],
        "exception": {"values": [{"stacktrace": {"frames": [
            {"filename": "/srv/app/services/integrations/zapier_v1/hooks.py", "in_app": True}]}}]}}}}
    assert client.post(hook, json=sentry, headers={"X-FeatureLedger-Secret": "wrong"}).status_code == 401
    r = client.post(hook, json=sentry, headers={"X-FeatureLedger-Secret": "sandbox", "Sentry-Hook-Resource": "error"})
    assert r.status_code == 202 and r.json()["signals"] == [{"id": "event:ci0001", "feature": "zapier-v1-webhooks",
                                                           "matched_by": "path"}], r.text
    r = client.post(f"/api/v1/sunset-plans/{zap}/advance", json={}, headers=A)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "errors_after_off"
    assert client.get(f"/api/v1/sunset-plans/{zap}", headers=A).json()["scream_test"]["errors_since_off"] == 1
    sig = client.get("/api/v1/damage-signals?feature_key=zapier-v1-webhooks", headers=A).json()
    assert len(sig) == 1 and "[email]" in sig[0]["title"] and "jane" not in sig[0]["title"]      # redacted
    assert client.post(f"/api/v1/damage-signals/{sig[0]['id']}/dismiss", json={"reason": "unrelated deploy"},
                       headers=P).status_code == 403                                             # admins only
    assert client.post(f"/api/v1/damage-signals/{sig[0]['id']}/dismiss", json={"reason": "unrelated deploy"},
                       headers=A).status_code == 200
    gw = {"signals": [{"id": "gw-ci-1", "kind": "deprecated_call", "route": "/hooks/v1/zapier/123456?token=abc", "count": 3}]}
    r = client.post(f"/api/v1/webhooks/signals/generic/{config.default_org_slug}", json=gw,
                    headers={"X-FeatureLedger-Secret": "sandbox"})
    assert r.status_code == 202 and r.json()["signals"][0]["matched_by"] == "route", r.text
    r = client.post(f"/api/v1/sunset-plans/{zap}/advance", json={}, headers=A)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "deprecated_route_called"
    call = client.get("/api/v1/damage-signals?feature_key=zapier-v1-webhooks", headers=A).json()[0]
    assert call["route"] == "/hooks/v1/zapier/:id" and call["occurrences"] == 3                   # no ids, no query
    assert client.post(f"/api/v1/damage-signals/{call['id']}/dismiss", json={"reason": "our own health check"},
                       headers=A).status_code == 200

    # 3) quiet: an admin approves -> the cleanup PR is opened on sunset/<feature_key>
    r = client.post(f"/api/v1/sunset-plans/{zap}/advance", json={}, headers=A)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["state"] == "dead_code_pr_opened"
    pr = p["cleanup_pr"]
    assert pr["branch"] == "sunset/zapier-v1-webhooks" and pr["provider"] == "sandbox" and pr["state"] == "open"
    assert pr["repo"] == "northwind/app" and pr["number"] > 1000 and pr["files_deleted"]
    assert all(f.startswith("services/integrations/zapier_v1/") for f in pr["files_deleted"])
    for needle in ("### Blast radius", "### Scream test", "### Audit trail", "Chain head", "zapier-v1"):
        assert needle in pr["body_markdown"]
    detail = client.get(f"/api/v1/sunset-plans/{zap}", headers=A).json()
    assert [x["state"] for x in detail["pull_requests"]] == ["open"]

    history_before = client.get("/api/v1/features/zapier-v1-webhooks", headers=A).json()["history"]
    assert len(history_before) == 6

    # 4) GitHub says the PR merged -> plan archived, feature archived, audit by the webhook
    integ = next(i for i in client.get("/api/v1/integrations", headers=A).json()["integrations"] if i["kind"] == "github")
    payload = {"action": "closed", "number": pr["number"], "repository": {"full_name": pr["repo"]},
               "pull_request": {"number": pr["number"], "merged": True, "merge_commit_sha": "c0ffee" * 6 + "c0ff",
                                "merged_at": "2026-11-18T12:00:00Z", "head": {"ref": pr["branch"]}}}
    bad = client.post(f"/api/v1/webhooks/github/{integ['id']}", json=payload,
                      headers={"X-GitHub-Event": "pull_request", "X-FeatureLedger-Secret": "wrong"})
    assert bad.status_code == 401
    r = client.post(f"/api/v1/webhooks/github/{integ['id']}", json=payload,
                    headers={"X-GitHub-Event": "pull_request", "X-FeatureLedger-Secret": "sandbox"})
    assert r.status_code == 202 and r.json()["archived"] is True, r.text
    final = client.get(f"/api/v1/sunset-plans/{zap}", headers=A).json()
    assert final["state"] == "archived" and final["cleanup_pr"]["state"] == "merged"
    assert final["pull_requests"][0]["state"] == "merged"
    last = final["audit_log"][-1]
    assert last["to_state"] == "archived" and last["payload"]["actor"] == "github-webhook"
    assert admin_sql("""SELECT f.status::text FROM features f JOIN organizations o ON o.id = f.org_id
                        WHERE f.key = 'zapier-v1-webhooks' AND o.slug = 'northwind'""")[0]["status"] == "archived"
    # replaying the webhook is harmless
    again = client.post(f"/api/v1/webhooks/github/{integ['id']}", json=payload,
                        headers={"X-GitHub-Event": "pull_request", "X-FeatureLedger-Secret": "sandbox"})
    assert again.json().get("already_merged") is True
    assert_hash_chain(final["audit_log"])

    # 5) archiving keeps the feature's monthly P&L history, even through a full recompute
    assert client.post("/api/v1/jobs/compute-pnl", headers=A).status_code == 200
    history_after = client.get("/api/v1/features/zapier-v1-webhooks", headers=A).json()["history"]
    money = ("period_month", "eng_hours", "c_eng", "c_support", "c_infra", "c_ai_tokens", "c_total", "v_usage",
             "classification")
    assert [{k: h[k] for k in money} for h in history_after] == [{k: h[k] for k in money} for h in history_before]
    assert len(history_after) == 6 and history_after[-1]["c_total"] > 0
    # the ledger hides it by default and shows it on request; the KPIs describe the live portfolio either way
    default = client.get("/api/v1/pnl?window=3", headers=A).json()
    shown = client.get("/api/v1/pnl?window=3&include_archived=true", headers=A).json()
    assert "zapier-v1-webhooks" not in {f["key"] for f in default["features"]}
    zap_row = next(f for f in shown["features"] if f["key"] == "zapier-v1-webhooks")
    assert zap_row["archived"] is True and zap_row["c_total"] > 0
    assert default["archived_count"] == shown["archived_count"] >= 1
    assert default["summary"] == shown["summary"] and default["summary"]["zombie_count"] == 2
    r = client.post("/api/v1/simulate", json={"feature_keys": ["zapier-v1-webhooks"]}, headers=A)
    assert r.status_code == 422 and "already archived" in r.text


def assert_hash_chain(log):
    for prev, cur in zip(log, log[1:]):
        assert cur["prev_hash"] == prev["hash"], "audit chain broken"


# ------------------------------------------------------------------------------------- canary + rollback
def test_full_canary_and_flag_rollback(client, tokens):
    A, P, C, V = (H(tokens[r]) for r in ("admin", "pm_tech_lead", "cs_lead", "viewer"))
    r = client.post("/api/v1/sunset-plans", json={"feature_key": "legacy-pdf-export", "reason": "Superseded by PDF export v2",
                                                  "scream_test_days": 7}, headers=P)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert client.post(f"/api/v1/sunset-plans/{pid}/evidence/blast_radius", headers=P).status_code == 200
    assert client.post(f"/api/v1/sunset-plans/{pid}/acknowledge", json={"kind": "inbound", "ids": ["*"]}, headers=P).status_code == 200
    assert client.post(f"/api/v1/sunset-plans/{pid}/advance", json={}, headers=P).json()["state"] == "blast_radius_checked"
    assert client.post(f"/api/v1/sunset-plans/{pid}/evidence/impact_dossier", headers=C).status_code == 200
    assert client.post(f"/api/v1/sunset-plans/{pid}/acknowledge", json={"kind": "account", "ids": ["*"]}, headers=C).status_code == 200
    r = client.post(f"/api/v1/sunset-plans/{pid}/advance", json={"cs_signoff": True}, headers=C)
    assert r.status_code == 200 and r.json()["state"] == "customer_outreach", r.text

    # admin approves the dark launch: flag -> zero_usage_off, on only for accounts with usage in 90 days
    assert client.post(f"/api/v1/sunset-plans/{pid}/advance", json={}, headers=C).status_code == 403
    r = client.post(f"/api/v1/sunset-plans/{pid}/advance", json={}, headers=A)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["canary_phase"]) == ("canary_dark_launch", "zero_usage_off")
    flag = client.get(f"/api/v1/sunset-plans/{pid}", headers=V).json()["flag"]
    assert flag["state"] == "zero_usage_off" and flag["provider"] == "sandbox" and flag["keep_on_for_count"] > 0

    # promote: fully off, scream test starts; advancing again is refused until it has run
    r = client.post(f"/api/v1/sunset-plans/{pid}/advance", json={}, headers=A)
    assert r.json()["canary_phase"] == "fully_off"
    r = client.post(f"/api/v1/sunset-plans/{pid}/advance", json={}, headers=A)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "scream_test_running"

    # rollback: viewers cannot; a CS lead can -> flag fully on, back to outreach, rollback_count 1
    assert client.post(f"/api/v1/sunset-plans/{pid}/rollback", json={"reason": "escalation"}, headers=V).status_code == 403
    r = client.post(f"/api/v1/sunset-plans/{pid}/rollback", json={"reason": "Enterprise escalation"}, headers=C)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["state"] == "customer_outreach" and p["rollback_count"] == 1 and p["canary_phase"] is None
    detail = client.get(f"/api/v1/sunset-plans/{pid}", headers=V).json()
    assert detail["flag"]["state"] == "fully_on"
    last = detail["audit_log"][-1]
    assert last["action"] == "rollback" and last["payload"]["flag_state"] == "fully_on"
    assert last["payload"]["actor_role"] == "cs_lead" and last["payload"]["rollback_number"] == 1
    assert_hash_chain(detail["audit_log"])
    flags = {f["flag_key"]: f for f in client.get("/api/v1/flags", headers=V).json()["flags"]}
    assert flags["legacy-pdf-export"]["state"] == "fully_on"

    # a second attempt: canary again, rolled back again -> rollback_count 2
    assert client.post(f"/api/v1/sunset-plans/{pid}/advance", json={}, headers=A).json()["state"] == "canary_dark_launch"
    r = client.post(f"/api/v1/sunset-plans/{pid}/rollback", json={"reason": "second escalation"}, headers=P)
    assert r.json()["rollback_count"] == 2
    # the audit log is append-only even for the database owner's app role
    with pytest.raises(asyncpg.exceptions.RaiseError):
        admin_sql("UPDATE sunset_audit_logs SET action = 'tampered' WHERE plan_id = $1", uuid.UUID(pid))
