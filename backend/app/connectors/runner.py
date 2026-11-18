"""Sync runner: connector batches -> ingest_inbox (+ cursor, same transaction) -> processors -> P&L.

Used by the ARQ worker (scheduled, manual and webhook-triggered syncs). `tx` is an org-scoped
transaction factory (app.db.org_tx in production), injectable for tests.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from ..config import config
from ..core.manifest import parse_manifest
from .base import Batch, Envelope, NotConfigured
from .processors import Stats, process_inbox
from .ratelimit import BucketStore
from .registry import SYNC_ORDER, build, fx_rates

log = logging.getLogger("featureledger.sync")

INSERT_ENVELOPE = """
    INSERT INTO ingest_inbox (idempotency_key, org_id, integration_id, entity_type, external_id, external_version, payload)
    VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (idempotency_key) DO NOTHING"""


async def write_envelopes(conn, org_id: str, envelopes: list[Envelope]) -> int:
    if not envelopes:
        return 0
    before = await conn.fetchval("SELECT count(*) FROM ingest_inbox WHERE idempotency_key = ANY($1)",
                                 [e.idempotency_key for e in envelopes])
    await conn.executemany(INSERT_ENVELOPE, [(e.idempotency_key, org_id, e.integration_id, e.entity_type, e.external_id,
                                              e.external_version, e.payload) for e in envelopes])
    return len({e.idempotency_key for e in envelopes}) - int(before)


async def commit_batch(conn, org_id: str, integration_id: str, batch: Batch) -> int:
    """Envelopes and the stream cursor in ONE transaction: a crash replays a batch, never skips one."""
    new = await write_envelopes(conn, org_id, batch.envelopes)
    if batch.cursor:
        await conn.execute("""
            INSERT INTO sync_cursors (integration_id, org_id, entity_type, cursor) VALUES ($1, $2, $3, $4)
            ON CONFLICT (integration_id, entity_type) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()""",
            integration_id, org_id, batch.stream, batch.cursor)
    return new


async def run_integration(tx: Callable[..., Any], org_id: str, integ: dict[str, Any], trigger: str,
                          store: BucketStore, manifest_text: str) -> dict[str, Any]:
    iid = str(integ["id"])
    async with tx(org_id) as conn:
        run_id = await conn.fetchval("""INSERT INTO sync_runs (org_id, integration_id, trigger) VALUES ($1, $2, $3)
                                        RETURNING id""", org_id, iid, trigger)
        cursors = {r["entity_type"]: str(r["cursor"]) for r in await conn.fetch(
            "SELECT entity_type, cursor FROM sync_cursors WHERE integration_id = $1", iid)}
    res = {"run_id": run_id, "kind": integ["kind"], "batches": 0, "seen": 0, "new": 0, "status": "succeeded",
           "error": None}
    try:
        built = build(integ["kind"], org_id, iid, integ.get("config") or {}, bool(integ.get("sandbox")), store,
                      manifest_text)
    except NotConfigured as e:
        res.update(status="failed", error=str(e))
        async with tx(org_id) as conn:
            await _finish(conn, res)
            await conn.execute("UPDATE integrations SET status = 'pending', last_error = $2 WHERE id = $1", iid, str(e))
        return res
    try:
        async for batch in built.connector.sync(built.ctx, cursors):
            async with tx(org_id) as conn:
                res["new"] += await commit_batch(conn, org_id, iid, batch)
            res["batches"] += 1
            res["seen"] += len(batch.envelopes)
    except Exception as e:  # noqa: BLE001 - recorded on the run; committed batches stay committed
        log.exception("sync %s failed", integ["kind"])
        res.update(status="partial" if res["batches"] else "failed", error=f"{type(e).__name__}: {e}"[:500])
    finally:
        await built.close()
    async with tx(org_id) as conn:
        await _finish(conn, res)
        if res["status"] == "succeeded":
            await conn.execute("""UPDATE integrations SET status = 'active', last_error = NULL, last_synced_at = now()
                                  WHERE id = $1""", iid)
        else:
            await conn.execute("UPDATE integrations SET status = 'error', last_error = $2 WHERE id = $1", iid,
                               res["error"])
    return res


async def _finish(conn, res: dict[str, Any]) -> None:
    await conn.execute("""UPDATE sync_runs SET status = $2::sync_status, finished_at = now(), batches = $3,
                          envelopes_seen = $4, envelopes_new = $5, error = $6 WHERE id = $1""",
                       res["run_id"], res["status"], res["batches"], res["seen"], res["new"], res["error"])


async def process_org(tx: Callable[..., Any], org_id: str, run_ids: list[int] | None = None) -> Stats:
    manifest = parse_manifest(config.manifest_path.read_text())
    async with tx(org_id) as conn:
        st = await process_inbox(conn, org_id, manifest, fx_rates())
        if run_ids:
            await conn.execute("UPDATE sync_runs SET processed = $2, dead_lettered = $3 WHERE id = ANY($1::bigint[])",
                               run_ids, st.processed, st.dead_lettered)
    return st


async def sync_org(tx: Callable[..., Any], org_id: str, integrations: list[dict[str, Any]], trigger: str,
                   store: BucketStore, recompute: Callable[[str], Awaitable[Any]] | None = None) -> dict[str, Any]:
    """Sync the given integrations in dependency order, then process the inbox, then recompute the P&L."""
    manifest_text = config.manifest_path.read_text()
    order = {k: i for i, k in enumerate(SYNC_ORDER)}
    results = []
    for integ in sorted(integrations, key=lambda i: order.get(i["kind"], 99)):
        results.append(await run_integration(tx, org_id, integ, trigger, store, manifest_text))
    st = await process_org(tx, org_id, [r["run_id"] for r in results])
    pnl = None
    if recompute and (st.processed or any(r["new"] for r in results)):
        pnl = await recompute(org_id)
    return {"runs": results, "processing": st.as_dict(), "pnl": pnl}
