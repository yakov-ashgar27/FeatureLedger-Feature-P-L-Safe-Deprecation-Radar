"""Integrations & Sync Health, manual triggers, dead letters, and inbound webhooks."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import config
from ..connectors import webhooks as wh
from ..connectors.base import SyncCtx
from ..connectors.ratelimit import MemoryBucketStore
from ..connectors.registry import build, live_configured
from ..connectors.runner import write_envelopes
from ..db import org_tx
from .deps import current_org, org_for, row

router = APIRouter(prefix="/api/v1", tags=["integrations"])
_arq = None


async def _queue():
    """ARQ pool, created lazily so the API still serves reads when Redis is down."""
    global _arq
    if _arq is None:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            _arq = await create_pool(RedisSettings.from_dsn(config.redis_url))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"the sync queue (Redis) is not reachable: {e}") from e
    return _arq


def _health(i: dict[str, Any], last_run: dict[str, Any] | None) -> str:
    if i["status"] == "disabled":
        return "disabled"
    if last_run and last_run["status"] == "running":
        return "syncing"
    if i["status"] == "pending" and not i["sandbox"] and not live_configured(i["kind"]):
        return "not_configured"
    if last_run is None:
        return "waiting"
    if last_run["status"] == "failed":
        return "failing"
    if last_run["status"] == "partial":
        return "degraded"
    last = i["last_synced_at"]
    if last and (datetime.now(timezone.utc) - last).total_seconds() > i["sync_interval_minutes"] * 60 * 3:
        return "stale"
    return "healthy"


@router.get("/integrations")
async def integrations(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = [dict(r) for r in await conn.fetch("""
            SELECT id, kind::text AS kind, status::text AS status, display_name, sandbox, sync_interval_minutes,
                   last_synced_at, last_error FROM integrations ORDER BY kind""")]
        cursors = await conn.fetch("SELECT integration_id, entity_type, cursor, updated_at FROM sync_cursors")
        inbox = await conn.fetch("""
            SELECT integration_id,
                   count(*) FILTER (WHERE processed_at IS NULL AND dead_lettered_at IS NULL) AS pending,
                   count(*) FILTER (WHERE processed_at IS NOT NULL) AS processed,
                   count(*) FILTER (WHERE dead_lettered_at IS NOT NULL) AS dead_letters
              FROM ingest_inbox GROUP BY integration_id""")
        manifest = await conn.fetchrow("""SELECT status, commit_sha, errors, received_at, source FROM manifest_versions
                                            ORDER BY received_at DESC, id DESC LIMIT 1""")
        runs = await conn.fetch("""
            SELECT * FROM (SELECT r.*, row_number() OVER (PARTITION BY integration_id ORDER BY started_at DESC) rn
                             FROM sync_runs r) x WHERE rn <= 8 ORDER BY started_at DESC""")
    by_c: dict[str, list] = {}
    for c in cursors:
        by_c.setdefault(str(c["integration_id"]), []).append(row(c))
    by_i = {str(r["integration_id"]): row(r) for r in inbox}
    by_r: dict[str, list] = {}
    for r in runs:
        by_r.setdefault(str(r["integration_id"]), []).append(dict(r))
    out = []
    for i in rows:
        iid = str(i["id"])
        recent = by_r.get(iid, [])
        d = row(i)
        d["health"] = "manual" if i["kind"] == "github" else _health(i, recent[0] if recent else None)
        d["live_configured"] = live_configured(i["kind"])
        d["cursors"] = by_c.get(iid, [])
        d["inbox"] = by_i.get(iid, {"pending": 0, "processed": 0, "dead_letters": 0})
        d["recent_runs"] = [row({k: v for k, v in r.items() if k not in ("rn", "org_id", "integration_id")})
                            for r in recent]
        if i["kind"] == "github":
            d["manifest"] = row(manifest) if manifest else None
            if manifest and manifest["status"] == "invalid":
                d["health"] = "failing"
        out.append(d)
    return {"integrations": out, "sandbox_mode": config.sandbox_mode}


async def _integration(conn, iid: str) -> dict[str, Any]:
    r = await conn.fetchrow("SELECT id, kind::text AS kind, config, sandbox, status::text AS status "
                            "FROM integrations WHERE id = $1", iid)
    if not r:
        raise HTTPException(404, "integration not found")
    return dict(r)


@router.post("/integrations/{integration_id}/sync", status_code=202)
async def trigger_sync(integration_id: str, org: str = Depends(org_for("integrations:manage"))):
    async with org_tx(org) as conn:
        i = await _integration(conn, integration_id)
    if i["kind"] == "github":
        raise HTTPException(422, "GitHub syncs through POST /api/v1/integrations/github/sync")
    q = await _queue()
    job = await q.enqueue_job("sync_org_job", org, [integration_id], "manual", _job_id=f"sync:{org}")
    return {"queued": job is not None,
            "message": "sync queued" if job else "a sync for this organization is already running; it will pick this up"}


@router.get("/integrations/{integration_id}/dead-letters")
async def dead_letters(integration_id: str, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("""SELECT idempotency_key, entity_type, external_id, attempts, last_error, received_at,
                                          dead_lettered_at FROM ingest_inbox
                                    WHERE integration_id = $1 AND dead_lettered_at IS NOT NULL
                                    ORDER BY dead_lettered_at DESC LIMIT 100""", integration_id)
    return [row(r) for r in rows]


@router.post("/integrations/{integration_id}/dead-letters/retry")
async def retry_dead_letters(integration_id: str, org: str = Depends(org_for("integrations:manage"))):
    async with org_tx(org) as conn:
        n = await conn.fetchval("""WITH x AS (UPDATE ingest_inbox SET dead_lettered_at = NULL, attempts = 0, last_error = NULL
                                    WHERE integration_id = $1 AND dead_lettered_at IS NOT NULL RETURNING 1)
                                    SELECT count(*) FROM x""", integration_id)
    q = await _queue()
    await q.enqueue_job("process_job", org, _job_id=f"process:{org}")
    return {"requeued": n}


# ----------------------------------------------------------------------------------------- webhooks
def _verify(kind: str, sandbox: bool, request: Request, body: bytes) -> None:
    h = request.headers
    if sandbox:
        wh.verify_shared_secret("sandbox", h.get("x-featureledger-secret"))
        return
    secret = os.environ.get({"stripe": "STRIPE_WEBHOOK_SECRET", "zendesk": "ZENDESK_WEBHOOK_SECRET",
                             "jira": "JIRA_WEBHOOK_SECRET", "posthog": "POSTHOG_WEBHOOK_SECRET"}.get(kind, "_"), "")
    if not secret:
        raise HTTPException(412, f"no webhook secret configured for {kind}")
    if kind == "stripe":
        wh.verify_stripe(secret, h.get("stripe-signature"), body)
    elif kind == "zendesk":
        wh.verify_zendesk(secret, h.get("x-zendesk-webhook-signature"), h.get("x-zendesk-webhook-signature-timestamp"), body)
    elif kind == "jira":
        wh.verify_hub_signature(secret, h.get("x-hub-signature"), body)
    else:
        wh.verify_shared_secret(secret, h.get("x-featureledger-secret"))


async def _github_webhook(i: dict[str, Any], request: Request, body: bytes, org: str) -> dict[str, Any]:
    """GitHub push -> features.yaml validate + apply (Phase 3). Other events are acknowledged and ignored."""
    import json

    from ..graph.manifest_sync import default_fetcher, handle_push
    h = request.headers
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    try:
        if secret:
            wh.verify_hub_signature(secret, h.get("x-hub-signature-256"), body)
        elif config.sandbox_mode:
            wh.verify_shared_secret("sandbox", h.get("x-featureledger-secret"))
        else:
            raise HTTPException(412, "set GITHUB_WEBHOOK_SECRET to accept GitHub webhooks")
    except wh.BadSignature as e:
        raise HTTPException(401, f"webhook rejected: {e}") from e
    event = h.get("x-github-event", "push")
    if event == "ping":
        return {"event": "ping", "ok": True}
    if event == "pull_request":
        return await _github_pull_request(json.loads(body or b"{}"), org)
    if event != "push":
        return {"event": event, "ignored": True}
    payload = json.loads(body or b"{}")
    try:
        fetcher = default_fetcher(payload, sandbox=bool(i["sandbox"]) or (config.sandbox_mode and not secret))
    except RuntimeError as e:
        raise HTTPException(412, str(e)) from e
    async with org_tx(org) as conn:
        res = await handle_push(conn, org, payload, fetcher)
    if res.applied:
        q = await _queue()
        await q.enqueue_job("graph_job", org, "manifest_push", _job_id=f"graph:{org}")
    return {"event": "push", "relevant": res.plan.relevant, "reason": res.plan.reason, "commit": res.plan.sha,
            "results": res.results}


async def _github_pull_request(payload: dict[str, Any], org: str) -> dict[str, Any]:
    """Phase 4: a merged cleanup PR (head branch sunset/<feature_key>) archives its sunset plan."""
    from ..execution.orchestrator import handle_pr_merged
    pr = payload.get("pull_request") or {}
    head = (pr.get("head") or {}).get("ref", "")
    if payload.get("action") != "closed" or not head.startswith("sunset/"):
        return {"event": "pull_request", "ignored": True, "reason": "not a closed sunset/* PR"}
    repo = (payload.get("repository") or {}).get("full_name") or ""
    number = int(payload.get("number") or pr.get("number") or 0)
    async with org_tx(org) as conn:
        if not pr.get("merged"):
            # closed without merging: the plan stays in dead_code_pr_opened until someone rolls back or reopens
            await conn.execute("""UPDATE cleanup_pull_requests SET state = 'closed'
                                   WHERE repo = $1 AND number = $2 AND state = 'open'""", repo, number)
            return {"event": "pull_request", "closed_unmerged": True, "repo": repo, "number": number}
        out = await handle_pr_merged(conn, org, repo, number, pr.get("merge_commit_sha"), pr.get("merged_at"))
    if out.get("archived"):
        try:
            q = await _queue()
            await q.enqueue_job("graph_job", org, "sunset_archived", _job_id=f"graph:{org}")
        except HTTPException:
            pass   # the hourly graph job recomputes anyway
    return {"event": "pull_request", **out}


@router.post("/webhooks/{kind}/{integration_id}", status_code=202)
async def webhook(kind: str, integration_id: str, request: Request):
    """No user session here: the tenant is resolved from the integration id, then the vendor signature
    (or, in SANDBOX_MODE, the shared sandbox secret) is verified before anything is read."""
    from ..db import integration_org
    org = await integration_org(integration_id)
    if not org:
        raise HTTPException(404, "integration not found")
    body = await request.body()
    async with org_tx(org) as conn:
        i = await _integration(conn, integration_id)
    if kind == "github" and i["kind"] == "github":
        return await _github_webhook(i, request, body, org)
    if i["kind"] != kind or kind == "aws_cost_explorer":
        raise HTTPException(404, "no webhook for this integration")
    try:
        _verify(kind, bool(i["sandbox"]), request, body)
    except wh.BadSignature as e:
        raise HTTPException(401, f"webhook rejected: {e}") from e
    built = build(kind, org, integration_id, i["config"] or {}, bool(i["sandbox"]), MemoryBucketStore(),
                  config.manifest_path.read_text()) if (i["sandbox"] or live_configured(kind)) else None
    ctx = built.ctx if built else SyncCtx(org, integration_id)
    envs = built.connector.handle_webhook(ctx, dict(request.headers), body) if built else []
    q = await _queue()
    if envs:
        async with org_tx(org) as conn:
            new = await write_envelopes(conn, org, envs)
        await q.enqueue_job("process_job", org, _job_id=f"process:{org}")
        return {"accepted": len(envs), "new": new, "action": "process"}
    await q.enqueue_job("sync_org_job", org, [integration_id], "webhook", _job_id=f"sync:{org}")
    return {"accepted": 0, "action": "sync"}
