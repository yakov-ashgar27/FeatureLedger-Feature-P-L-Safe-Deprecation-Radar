"""Feature flags under FeatureLedger control: state, evaluation counts, Statsig exposure webhook, and the
SANDBOX_MODE injection endpoint used to rehearse a scream (an 'on' evaluation during the test)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.session import Principal, require
from ..config import config
from ..db import org_tx, resolve_org
from ..execution import orchestrator as ox
from ..flags.base import EvalBucket
from ..flags.registry import provider_name
from .deps import current_org, org_for, row

router = APIRouter(prefix="/api/v1/flags", tags=["flags"])


@router.get("")
async def list_flags(org: str = Depends(current_org)):
    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with org_tx(org) as conn:
        rows = await conn.fetch("""
            SELECT f.key AS feature_key, f.name AS feature_name, f.flag_key, f.status::text AS feature_status,
                   coalesce(s.state, 'fully_on') AS state, s.provider, coalesce(jsonb_array_length(s.keep_on_for), 0)
                   AS keep_on_for_count, s.updated_at, s.last_error, s.plan_id,
                   coalesce(e.on_n, 0) AS on_7d, coalesce(e.off_n, 0) AS off_7d
              FROM features f
              LEFT JOIN flag_states s ON s.flag_key = f.flag_key
              LEFT JOIN (SELECT flag_key, sum(on_count)::bigint on_n, sum(off_count)::bigint off_n FROM flag_evaluations
                          WHERE bucket_start >= $1 GROUP BY flag_key) e ON e.flag_key = f.flag_key
             WHERE f.flag_key IS NOT NULL ORDER BY (s.state IS NULL), f.key""", since)
    return {"provider": provider_name(), "flags": [row(r) for r in rows]}


@router.get("/{flag_key}/evaluations")
async def evaluations(flag_key: str, days: int = 30, org: str = Depends(current_org)):
    since = datetime.now(timezone.utc) - timedelta(days=min(max(days, 1), 180))
    async with org_tx(org) as conn:
        rows = await conn.fetch("""SELECT bucket_start, source, on_count, off_count FROM flag_evaluations
                                    WHERE flag_key = $1 AND bucket_start >= $2 ORDER BY bucket_start""", flag_key, since)
    return [row(r) for r in rows]


class Inject(BaseModel):
    on_count: int = Field(0, ge=0, le=10_000_000)
    off_count: int = Field(0, ge=0, le=10_000_000)
    at: datetime | None = None
    reset: bool = False


@router.post("/{flag_key}/evaluations")
async def inject(flag_key: str, body: Inject, who: Principal = Depends(require("flags:simulate"))):
    """SANDBOX_MODE: record evaluation counts as if the provider had reported them (reset=true removes
    every injected bucket). With a live provider, counts only come from the provider."""
    if not config.sandbox_mode:
        raise HTTPException(404, "only available with SANDBOX_MODE=true")
    async with org_tx(who.org_id) as conn:
        if body.reset:
            n = await conn.fetchval("""SELECT count(*) FROM flag_evaluations
                                        WHERE flag_key = $1 AND source = 'sandbox-injected'""", flag_key)
            await conn.execute("DELETE FROM flag_evaluations WHERE flag_key = $1 AND source = 'sandbox-injected'",
                               flag_key)
            return {"reset": True, "removed_buckets": n}
        at = (body.at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        await ox.record_buckets(conn, who.org_id, flag_key, [EvalBucket(at, body.on_count, body.off_count)],
                                "sandbox-injected", accumulate=True)
        alerts = await ox.scan_canaries(conn, who.org_id)
    return {"recorded": {"bucket_start": at.isoformat(), "on_count": body.on_count, "off_count": body.off_count},
            "canaries": alerts}


@router.post("/ingest")
async def ingest_now(org: str = Depends(org_for("integrations:manage"))):
    """Pull evaluation counts for every plan in canary now (the worker also does this every 15 minutes)."""
    async with org_tx(org) as conn:
        try:
            return {"canaries": await ox.scan_canaries(conn, org)}
        except ox.ExecutionError as e:
            raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from e


@router.post("/webhooks/statsig", status_code=202)
async def statsig_webhook(request: Request, org: str):
    """Statsig Event Webhook (gate exposures). Verified with STATSIG_WEBHOOK_SECRET, or the sandbox secret."""
    from ..flags.statsig import BadStatsigSignature, exposures_to_buckets, verify_statsig
    body = await request.body()
    secret = os.environ.get("STATSIG_WEBHOOK_SECRET", "")
    h = request.headers
    try:
        if secret:
            verify_statsig(secret, h.get("x-statsig-signature"), h.get("x-statsig-request-timestamp"), body)
        elif not (config.sandbox_mode and h.get("x-featureledger-secret") == "sandbox"):
            raise HTTPException(412, "set STATSIG_WEBHOOK_SECRET to accept Statsig webhooks")
    except BadStatsigSignature as e:
        raise HTTPException(401, f"webhook rejected: {e}") from e
    org_id = await resolve_org(org)
    if not org_id:
        raise HTTPException(404, "unknown organization")
    per_gate = exposures_to_buckets(body)
    async with org_tx(org_id) as conn:
        known = {r["flag_key"] for r in await conn.fetch("SELECT flag_key FROM features WHERE flag_key IS NOT NULL")}
        n = 0
        for gate, buckets in per_gate.items():
            if gate in known:   # ignore gates FeatureLedger does not map to a feature
                n += await ox.record_buckets(conn, org_id, gate, buckets, "statsig", accumulate=True)
    return {"buckets": n, "gates": sorted(g for g in per_gate if g in known)}
