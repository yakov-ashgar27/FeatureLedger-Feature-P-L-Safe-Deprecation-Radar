"""Live demo org (DEMO_MODE=true): "Acme Cloud", the seed company re-keyed into its own tenant.

The demo org is a normal tenant behind RLS, with three extra properties:
  * organizations.is_demo = true, which is the only kind of org /api/v1/demo/reset may delete;
  * every id is a deterministic uuid5 of the seed's id under DEMO_NS, so a reset restores the very same
    plans, users and integration ids (open browser tabs keep working);
  * flags, cleanup PRs, PR comments and Slack always use sandbox providers in this org, whatever the
    deployment is configured with (app/execution/orchestrator.providers_for, app/db.org_is_demo).
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from .config import config

log = logging.getLogger("featureledger.demo")
DEMO_NS = uuid.UUID("2d0c3d6e-6a0f-4c55-9d44-5ad3c0de0a01")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DEMO_LABEL = "Launch Live Demo ({name} — {engineers} Engineers)"
ORPHAN_TABLES_SQL = """
    SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'org_id'
     WHERE c.relkind IN ('r', 'p') AND c.relnamespace = 'public'::regnamespace AND NOT c.relispartition
       AND c.relname <> 'organizations'
       AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'f'
                                                      AND k.confrelid = 'organizations'::regclass)"""


def demo_id(seed_id: str) -> str:
    return str(uuid.uuid5(DEMO_NS, seed_id))


def demo_seed_sql(seed_sql: str, slug: str, name: str) -> str:
    """Re-key the deterministic seed into the demo tenant (full data set: no connector sync needed)."""
    sql = UUID_RE.sub(lambda m: demo_id(m.group(0)), seed_sql)
    sql = sql.replace(",'Northwind Cloud','northwind')", f",'{name.replace(chr(39), chr(39) * 2)}','{slug}')")
    sql = sql.replace("@northwind.io", "@acme-cloud.demo").replace("northwind/app", "acme/app")
    sql = sql.replace("GitHub · northwind", "GitHub · acme")
    # sandbox AWS Cost Explorer owns all cost lines in the demo, so a "Trigger Sync" rebuilds instead of adding
    sql = sql.replace("'openai_usage'", "'aws_cost_explorer'")
    return "\n".join(ln for ln in sql.splitlines() if ln.strip() not in ("BEGIN;", "COMMIT;"))


async def reset_demo(dsn: str | None = None) -> dict:
    """Delete the demo org (only if it is flagged is_demo) and seed it again; then compute its P&L."""
    import asyncpg
    from .db import _init_conn
    from .jobs.compute_pnl import recompute_org
    from .seed.generate import build
    t0 = time.monotonic()
    seed = build(config.manifest_path.read_text())
    org_id = demo_id(seed.org_id)
    sql = demo_seed_sql(seed.sql, config.demo_org_slug, config.demo_org_name)
    conn = await asyncpg.connect(dsn or config.admin_database_url, server_settings={"jit": "off"})
    try:
        await _init_conn(conn)
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('featureledger:demo-reset'))")
            for r in await conn.fetch("SELECT id, is_demo FROM organizations WHERE slug = $1 OR id = $2::uuid",
                                      config.demo_org_slug, org_id):
                if not r["is_demo"]:
                    raise RuntimeError(f"organization {config.demo_org_slug!r} is a real tenant; refusing to reset it")
                await conn.execute("SELECT set_config('featureledger.purge_org', $1, true)", str(r["id"]))
                await conn.execute("DELETE FROM sunset_audit_logs WHERE org_id = $1", r["id"])
                # partitioned fact tables carry org_id without a foreign key: the cascade cannot reach them
                for t in await conn.fetch(ORPHAN_TABLES_SQL):
                    await conn.execute(f'DELETE FROM "{t["relname"]}" WHERE org_id = $1', r["id"])
                await conn.execute("DELETE FROM organizations WHERE id = $1", r["id"])
            await conn.execute(sql)
            await conn.execute("UPDATE organizations SET is_demo = true WHERE id = $1::uuid", org_id)
            await conn.execute("""UPDATE integrations SET sandbox = true, status = 'active', last_error = NULL
                                   WHERE org_id = $1::uuid AND kind <> 'github'""", org_id)
    finally:
        await conn.close()
    converged = await converge(org_id)
    pnl = converged.get("pnl") or await recompute_org(org_id)   # app role + RLS, never the admin connection
    took = round(time.monotonic() - t0, 1)
    log.info("demo org %s reset in %ss: %s", config.demo_org_slug, took, pnl.get("classes"))
    return {"org_id": org_id, "slug": config.demo_org_slug, "seconds": took, "pnl": pnl,
            "graph": {k: converged["graph"][k] for k in ("tier2", "tier3")} if converged.get("graph") else None}


async def converge(org_id: str) -> dict:
    """Run one in-process sandbox sync + Feature Graph pass so the demo starts at its steady state.

    The seed SQL is the *pre-inference* state: it carries the facts but not what Tier 2 (code clusters) and
    Tier 3 (ticket text) infer on the first sync, nor the Jira issues and the `__unresolved__` usage account.
    Without this step the visitor's first "Trigger Sync" moved ~155 engineering hours and ~79 tickets onto
    features (+0.4% monthly cost). After it, any further sync over unchanged fixtures changes nothing.
    """
    from .connectors.ratelimit import MemoryBucketStore
    from .connectors.runner import sync_org
    from .db import org_tx
    from .jobs.compute_pnl import recompute_org
    async with org_tx(org_id) as conn:
        integs = [dict(r) for r in await conn.fetch("""SELECT id, kind::text AS kind, config, sandbox FROM integrations
                                                       WHERE status <> 'disabled' AND kind <> 'github' AND sandbox""")]
    if not integs:
        return {}
    return await sync_org(org_tx, org_id, integs, "manual", MemoryBucketStore(), recompute_org)


async def ensure_demo(conn) -> str | None:
    """At startup (migrate): create the demo org once when DEMO_MODE is on."""
    if not config.demo_mode:
        return None
    if await conn.fetchval("SELECT 1 FROM organizations WHERE slug = $1 AND is_demo", config.demo_org_slug):
        return None
    if await conn.fetchval("SELECT 1 FROM organizations WHERE slug = $1", config.demo_org_slug):
        raise SystemExit(f"DEMO_ORG_SLUG {config.demo_org_slug!r} is taken by a real organization")
    return (await reset_demo())["org_id"]
