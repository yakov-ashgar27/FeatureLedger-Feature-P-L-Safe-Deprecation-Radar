"""ARQ worker: scheduled syncs (every 15 minutes), manual and webhook triggers, inbox processing, P&L.

    arq app.worker.WorkerSettings

Jobs are de-duplicated per org (`_job_id = sync:<org>`), so a manual trigger during a scheduled run is a
no-op instead of a race. Rate limits are shared across worker processes through Redis token buckets.
"""
from __future__ import annotations

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from arq.worker import func

from .config import config
from .connectors.ratelimit import RedisBucketStore
from .connectors.runner import process_org, sync_org
from .db import close_pool, init_pool, org_tx
from .graph.jobs import run_graph
from .jobs.compute_pnl import recompute

log = logging.getLogger("featureledger.worker")


async def _recompute(org_id: str) -> dict:
    async with org_tx(org_id) as conn:
        return await recompute(conn, org_id)


async def _targets() -> list[dict[str, Any]]:
    pool = await init_pool()
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch("SELECT * FROM list_sync_targets()")]


async def _integrations(org_id: str, ids: list[str] | None) -> list[dict[str, Any]]:
    async with org_tx(org_id) as conn:
        rows = await conn.fetch("""SELECT id, kind::text AS kind, config, sandbox FROM integrations
                                    WHERE status <> 'disabled' AND kind <> 'github'
                                      AND ($1::uuid[] IS NULL OR id = ANY($1::uuid[]))""", ids)
    return [dict(r) for r in rows]


async def sync_org_job(ctx: dict, org_id: str, integration_ids: list[str] | None = None, trigger: str = "schedule"):
    integs = await _integrations(org_id, integration_ids)
    if not integs:
        return {"skipped": "no integrations"}
    out = await sync_org(org_tx, org_id, integs, trigger, RedisBucketStore(ctx["redis"]), _recompute)
    log.info("sync %s (%s): %s", org_id, trigger, {k: v for k, v in out.items() if k != "runs"})
    return out


async def process_job(ctx: dict, org_id: str):
    st = await process_org(org_tx, org_id)
    if st.processed:
        await _recompute(org_id)
    return st.as_dict()


async def graph_job(ctx: dict, org_id: str, reason: str = "manual"):
    """Re-run the Feature Graph (reallocation + Tier 2; Tier 3 needs a sync's text) and the P&L."""
    async with org_tx(org_id) as conn:
        out = await run_graph(conn, org_id)
    await _recompute(org_id)
    log.info("graph %s (%s): %s", org_id, reason, out)
    return out


async def hourly_graph(ctx: dict) -> dict:
    pool = await init_pool()
    async with pool.acquire() as conn:
        orgs = [str(r["org_id"]) for r in await conn.fetch("SELECT org_id FROM list_orgs()")]
    for org in orgs:
        await ctx["redis"].enqueue_job("graph_job", org, "hourly", _job_id=f"graph:{org}")
    return {"orgs": len(orgs)}


async def schedule_due_syncs(ctx: dict) -> dict:
    due: dict[str, list[str]] = {}
    for t in await _targets():
        if t["due"]:
            due.setdefault(str(t["org_id"]), []).append(str(t["integration_id"]))
    for org, ids in due.items():
        await ctx["redis"].enqueue_job("sync_org_job", org, ids, "schedule", _job_id=f"sync:{org}")
    return {"orgs": len(due), "integrations": sum(len(v) for v in due.values())}


async def startup(ctx: dict) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await init_pool()


async def shutdown(ctx: dict) -> None:
    await close_pool()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(config.redis_url)
    functions = [func(sync_org_job, keep_result=0, timeout=1800, max_tries=1),
                 func(process_job, keep_result=0, timeout=900, max_tries=1),
                 func(graph_job, keep_result=0, timeout=900, max_tries=1)]
    cron_jobs = [cron(schedule_due_syncs, minute={0, 15, 30, 45}, run_at_startup=True),
                 cron(hourly_graph, minute=7)]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4
    job_timeout = 1800
