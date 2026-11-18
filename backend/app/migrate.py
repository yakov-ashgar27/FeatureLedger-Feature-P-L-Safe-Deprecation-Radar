"""Apply SQL migrations, provision the RLS-bound app role, seed, and compute the first P&L.

    python -m app.migrate            # migrate (+ seed and compute if SEED_ON_START and DB is empty)
    python -m app.migrate --no-seed
Runs as the schema owner (ADMIN_DATABASE_URL). Idempotent: applied files are tracked by name + sha256.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging

import asyncpg

from .config import config
from .db import _init_conn

log = logging.getLogger("featureledger.migrate")
APP_ROLE = "featureledger_app"


async def apply_migrations(conn: asyncpg.Connection) -> list[str]:
    await conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
    done = {r["name"]: r["sha256"] for r in await conn.fetch("SELECT name, sha256 FROM schema_migrations")}
    applied = []
    for path in sorted(config.migrations_dir.glob("*.sql")):
        sql = path.read_text()
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if path.name in done:
            if done[path.name] != digest:
                raise RuntimeError(f"migration {path.name} changed after being applied; add a new migration instead")
            continue
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (name, sha256) VALUES ($1, $2)", path.name, digest)
        applied.append(path.name)
        log.info("applied %s", path.name)
    return applied


async def provision_app_role(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE)
    pw = config.app_db_password.replace("'", "''")
    if not exists:
        await conn.execute(f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{pw}'")
    else:
        await conn.execute(f"ALTER ROLE {APP_ROLE} WITH LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{pw}'")
    await conn.execute(f"""
        GRANT USAGE ON SCHEMA public TO {APP_ROLE};
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
        GRANT EXECUTE ON FUNCTION resolve_org_slug(text) TO {APP_ROLE};
        REVOKE UPDATE, DELETE ON sunset_audit_logs FROM {APP_ROLE};
        REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM {APP_ROLE};
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};
    """)


async def seed_if_empty(conn: asyncpg.Connection) -> str | None:
    if await conn.fetchval("SELECT count(*) FROM organizations"):
        return None
    from .seed.generate import build
    seed = build(config.manifest_path.read_text())
    await conn.execute(seed.sql)  # the script carries its own BEGIN/COMMIT
    log.info("seeded org %s", seed.org_id)
    return seed.org_id


async def bootstrap_real_org(conn: asyncpg.Connection) -> str:
    """Real-data mode: create the org named by DEFAULT_ORG_SLUG/ORG_NAME and apply features.yaml."""
    from .bootstrap import apply_manifest, ensure_org
    from .core.manifest import parse_manifest
    manifest = parse_manifest(config.manifest_path.read_text())
    async with conn.transaction():
        org_id = await ensure_org(conn, config.default_org_slug, config.org_name)
        stats = await apply_manifest(conn, org_id, manifest)
    log.info("org %s (%s): applied features.yaml %s", config.default_org_slug, org_id, stats)
    return org_id


async def main(seed: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for attempt in range(30):  # wait for postgres in docker-compose
        try:
            conn = await asyncpg.connect(config.admin_database_url)
            break
        except (OSError, asyncpg.CannotConnectNowError):
            await asyncio.sleep(1 + attempt * 0.2)
    else:
        raise SystemExit("database not reachable")
    try:
        await _init_conn(conn)
        await apply_migrations(conn)
        await provision_app_role(conn)
        if seed:
            org_id = await seed_if_empty(conn)
        else:
            org_id = await bootstrap_real_org(conn)
    finally:
        await conn.close()
    if org_id:
        from .jobs.compute_pnl import recompute_org
        summary = await recompute_org(org_id, dsn=config.admin_database_url)
        log.info("computed P&L: %s", summary)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-seed", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(seed=config.seed_on_start and not args.no_seed))
