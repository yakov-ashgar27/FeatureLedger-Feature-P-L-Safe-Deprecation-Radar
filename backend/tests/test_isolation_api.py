"""Tenant isolation, checked mechanically against the real database and every API route.

    FL_INTEGRATION=1 SANDBOX_MODE=true DATABASE_URL=<app role> ADMIN_DATABASE_URL=<owner> pytest tests/test_isolation_api.py

Layers checked:
  1. catalog: every table is RLS-enabled AND forced, with a tenant policy; partitions are unreachable
     for the application role; the application role cannot bypass RLS and owns nothing;
  2. privileged code: the exact inventory of SECURITY DEFINER functions, each with a pinned search_path
     and no EXECUTE for PUBLIC;
  3. behaviour: with no tenant set the application role sees zero rows in every table;
  4. API: every route the app exposes (enumerated from FastAPI itself, so new routes are covered
     automatically) is called by an admin of an empty second tenant with the first tenant's ids;
     no response may contain the first tenant's ids, emails or name, and nothing may 500.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from types import SimpleNamespace

import pytest

if os.environ.get("FL_INTEGRATION") != "1":
    pytest.skip("integration suite: set FL_INTEGRATION=1 with a migrated, seeded database", allow_module_level=True)

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth.session import issue_session  # noqa: E402
from app.config import config  # noqa: E402
from app.main import app  # noqa: E402

APP_ROLE = "featureledger_app"
NO_TENANT_DATA = {"schema_migrations"}          # bookkeeping only: no org_id, no customer data
PRIVILEGED = {"resolve_org_slug", "list_sync_targets", "list_orgs", "auth_get_user", "auth_dev_user",
              "auth_upsert_user", "integration_org"}
PUBLIC_ROUTES = re.compile(r"^/(health|docs|redoc|openapi\.json)|^/api/v1/(auth|demo)/|^/api/v1/webhooks/|"
                           r"^/api/v1/flags/webhooks/")


def sql(query: str, *args, dsn: str | None = None):
    async def go():
        conn = await asyncpg.connect(dsn or config.admin_database_url)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


# ------------------------------------------------------------------------------------------ catalog
def test_every_table_has_forced_row_level_security():
    rows = sql("""
        SELECT c.relname, c.relkind, c.relispartition, c.relrowsecurity, c.relforcerowsecurity,
               EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'org_id'
                                                      AND NOT a.attisdropped) AS has_org,
               (SELECT string_agg(pg_get_expr(p.polqual, p.polrelid), ' ') FROM pg_policy p
                 WHERE p.polrelid = c.oid) AS quals
          FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')""")
    problems = []
    for r in rows:
        if r["relname"] in NO_TENANT_DATA:
            continue
        if not (r["relrowsecurity"] and r["relforcerowsecurity"]):
            problems.append(f"{r['relname']}: RLS enabled={r['relrowsecurity']} forced={r['relforcerowsecurity']}")
        elif "app.org_id" not in (r["quals"] or ""):
            problems.append(f"{r['relname']}: no tenant policy ({r['quals']})")
        elif r["relname"] != "organizations" and not r["has_org"]:
            problems.append(f"{r['relname']}: no org_id column to scope by")
    assert not problems, "\n".join(problems)
    assert len(rows) > 60          # 36 tables + monthly partitions: the query really saw the schema


def test_partitions_are_unreachable_for_the_app_role():
    rows = sql("""SELECT c.relname,
                         has_table_privilege($1, c.oid, 'SELECT') OR has_table_privilege($1, c.oid, 'INSERT')
                         OR has_table_privilege($1, c.oid, 'UPDATE') OR has_table_privilege($1, c.oid, 'DELETE') AS any
                    FROM pg_class c WHERE c.relispartition AND c.relkind = 'r'""", APP_ROLE)
    assert rows and not [r["relname"] for r in rows if r["any"]]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        sql("SELECT count(*) FROM usage_signals_2026_10", dsn=config.database_url)


def test_app_role_cannot_bypass_rls_or_own_tables():
    r = sql("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = $1", APP_ROLE)[0]
    assert not any(r.values())
    owned = sql("SELECT relname FROM pg_class WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = $1)", APP_ROLE)
    assert owned == []          # an owner would be exempt from non-forced policies


def test_privileged_functions_are_an_exact_audited_inventory():
    rows = sql("""SELECT p.proname, p.proconfig, has_function_privilege('public', p.oid, 'EXECUTE') AS public_exec
                    FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef""")
    assert {r["proname"] for r in rows} == PRIVILEGED, "a SECURITY DEFINER function was added: review it, then list it"
    for r in rows:
        assert any(c.startswith("search_path=") for c in (r["proconfig"] or [])), f"{r['proname']}: unpinned search_path"
        assert not r["public_exec"], f"{r['proname']}: executable by PUBLIC"


def test_app_role_without_tenant_sees_no_rows_anywhere():
    tables = [r["relname"] for r in sql("""SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace
                                             AND relkind IN ('r', 'p') AND NOT relispartition""")
              if r["relname"] not in NO_TENANT_DATA]
    union = " UNION ALL ".join(f"SELECT '{t}' AS t, count(*) AS n FROM \"{t}\"" for t in tables)
    seen = {r["t"]: r["n"] for r in sql(union, dsn=config.database_url)}
    assert len(seen) == len(tables) and not {t: n for t, n in seen.items() if n}
    total = sql("SELECT (SELECT count(*) FROM usage_signals) + (SELECT count(*) FROM work_items) AS n")[0]["n"]
    assert total > 1000             # there is plenty of data to leak, and none did


# ------------------------------------------------------------------------------------------ API sweep
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _seed_signal(org_id) -> list[str]:
    """A damage signal in tenant A, so the sweep has a real foreign id to try (removed by the fixture)."""
    return [str(sql("""INSERT INTO damage_signals (org_id, source, kind, external_id, matched_by, occurred_at)
                       VALUES ($1, 'generic', 'error', 'isolation-probe', 'none', now() - interval '400 days')
                       ON CONFLICT (org_id, source, external_id) DO UPDATE SET occurrences = 1
                       RETURNING id""", org_id)[0]["id"])]


@pytest.fixture(scope="module")
def tenant_a():
    org = sql("SELECT id, name FROM organizations WHERE slug = $1", config.default_org_slug)[0]
    ids = lambda q: [str(r[0]) for r in sql(q, org["id"])]   # noqa: E731
    return {
        "org": str(org["id"]), "name": org["name"],
        "plan": ids("SELECT id FROM sunset_plans WHERE org_id = $1 ORDER BY created_at LIMIT 3"),
        "user": ids("SELECT id FROM users WHERE org_id = $1"),
        "integration": ids("SELECT id FROM integrations WHERE org_id = $1"),
        "suggestion": ids("SELECT id FROM mapping_suggestions WHERE org_id = $1 LIMIT 5"),
        "feature_id": ids("SELECT id FROM features WHERE org_id = $1"),
        "account": ids("SELECT id FROM accounts WHERE org_id = $1 LIMIT 300"),
        "team": ids("SELECT id FROM teams WHERE org_id = $1"),
        "signal": ids("SELECT id FROM damage_signals WHERE org_id = $1") or _seed_signal(org["id"]),
        "emails": [r[0] for r in sql("SELECT email FROM users WHERE org_id = $1", org["id"])],
        "feature": [r[0] for r in sql("SELECT key FROM features WHERE org_id = $1 ORDER BY key", org["id"])],
        "flag": [r[0] for r in sql("SELECT flag_key FROM features WHERE org_id = $1 AND flag_key IS NOT NULL",
                                   org["id"])],
    }


@pytest.fixture(scope="module")
def tenant_b():
    org = sql("INSERT INTO organizations (name, slug) VALUES ('Isolation Probe', $1) RETURNING id",
              f"probe-{uuid.uuid4().hex[:8]}")[0]["id"]
    sql("INSERT INTO org_settings (org_id) VALUES ($1)", org)
    uid = sql("""INSERT INTO users (org_id, email, name, role) VALUES ($1, 'root@probe.example', 'Probe', 'admin')
                 RETURNING id""", org)[0]["id"]
    return issue_session({"id": uid, "org_id": org}, "test")


PARAM_VALUES = {
    "plan_id": lambda a: a["plan"][0], "user_id": lambda a: a["user"][0], "integration_id": lambda a: a["integration"][0],
    "suggestion_id": lambda a: a["suggestion"][0] if a["suggestion"] else str(uuid.uuid4()),
    "key": lambda a: a["feature"][0], "feature_key": lambda a: a["feature"][0], "flag_key": lambda a: a["flag"][0],
    "kind": lambda a: "blast_radius",
    "signal_id": lambda a: a["signal"][0],
}
# bodies that reach the handler (not just pydantic) for routes whose bodies are required
BODIES = {
    "/api/v1/simulate": {"feature_keys": ["legacy-pdf-export"]},
    "/api/v1/sunset-plans": {"feature_key": "legacy-pdf-export", "reason": "isolation probe only"},
    "/api/v1/sunset-plans/{plan_id}/acknowledge": {"kind": "inbound", "ids": ["*"]},
    "/api/v1/sunset-plans/{plan_id}/rollback": {"reason": "isolation probe"},
    "/api/v1/sunset-plans/{plan_id}/cancel": {"reason": "isolation probe"},
    "/api/v1/triage/{suggestion_id}": {"decision": "reject"},
    "/api/v1/zombie-guard/preview": {"paths": ["services/export/pdf_v1/render.py"]},
    "/api/v1/users/{user_id}": {"role": "viewer"},
    "/api/v1/flags/{flag_key}/evaluations": {"on_count": 1},
    "/api/v1/settings": {"eng_hourly_rate_usd": 1999},
    "/api/v1/damage-signals/{signal_id}/dismiss": {"reason": "isolation probe"},
}
SKIP = {("POST", "/api/v1/integrations/github/sync")}   # would call api.github.com with the deployment token


def secrets_of(a) -> list[str]:
    # (the org name is not a secret here: it also sits in the shared sample features.yaml comment)
    vals = [a["org"], *a["emails"]]
    for k in ("plan", "user", "integration", "suggestion", "feature_id", "account", "team", "signal"):
        vals += a[k]
    return [v for v in vals if v]


def test_every_route_refuses_a_foreign_tenant(client, tenant_a, tenant_b):
    h = {"Authorization": f"Bearer {tenant_b}"}
    secrets = secrets_of(tenant_a)
    tested, failures = 0, []
    # every operation the API publishes (the OpenAPI schema is stable across FastAPI versions; no route is
    # hidden from it, so a new route is swept automatically)
    ops = {path: {m.upper() for m in item if m in ("get", "post", "put", "patch", "delete")}
           for path, item in app.openapi()["paths"].items()}
    routes = [SimpleNamespace(path=p, methods=ms) for p, ms in ops.items() if not PUBLIC_ROUTES.match(p)]
    expected = sum(1 for r in routes for m in r.methods if (m, r.path) not in SKIP)
    for route in routes:
        for method in sorted(route.methods):
            if (method, route.path) in SKIP:
                continue
            params = re.findall(r"{(\w+)(?::[^}]*)?}", route.path)
            missing = [p for p in params if p not in PARAM_VALUES]
            assert not missing, f"{route.path}: teach the sweep a value for {missing}"
            sent = {p: PARAM_VALUES[p](tenant_a) for p in params}
            path = route.path
            for p, v in sent.items():
                path = re.sub(r"{" + p + r"(?::[^}]*)?}", v, path)
            body = BODIES.get(route.path, {}) if method in ("POST", "PUT", "PATCH") else None
            r = client.request(method, path, json=body, headers=h)
            tested += 1
            text = r.text
            for v in sent.values():          # an echo of what we sent is not a leak
                text = text.replace(v, "")
            leaked = [s for s in secrets if s in text]
            if r.status_code >= 500 or leaked:
                failures.append(f"{method} {path} -> {r.status_code} leaked={leaked[:3]} {text[:160]}")
    assert tested == expected and tested >= 40, f"{tested} of {expected} authenticated route calls made"
    assert not failures, "\n".join(failures)


def test_foreign_ids_are_not_found_and_headers_cannot_switch_tenant(client, tenant_a, tenant_b):
    h = {"Authorization": f"Bearer {tenant_b}", "X-Org-Slug": config.default_org_slug}
    for pid in tenant_a["plan"]:
        assert client.get(f"/api/v1/sunset-plans/{pid}", headers=h).status_code == 404
    assert client.get("/api/v1/sunset-plans", headers=h).json() == []
    assert client.get("/api/v1/users", headers=h).json()[0]["email"] == "root@probe.example"
    assert client.get("/api/v1/integrations", headers=h).json()["integrations"] == []
