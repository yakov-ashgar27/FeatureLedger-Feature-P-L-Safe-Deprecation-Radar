"""Feature Graph triage, manifest validation, jobs and connector triggers."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from ..config import config
from ..core.manifest import ManifestError, parse_manifest
from ..db import org_tx
from ..jobs.compute_pnl import recompute
from .deps import current_org, row

router = APIRouter(prefix="/api/v1", tags=["admin"])
log = logging.getLogger("featureledger.api")


@router.get("/triage")
async def triage_queue(status: Literal["pending", "confirmed", "rejected"] = "pending",
                       org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("""
            SELECT s.id, s.subject_type::text AS subject_type, s.subject_id, s.subject_label, s.confidence,
                   s.source::text AS source, s.rationale, s.status::text AS status, s.created_at,
                   f.key AS suggested_feature_key, f.name AS suggested_feature_name
              FROM mapping_suggestions s LEFT JOIN features f ON f.id = s.suggested_feature_id
             WHERE s.status = $1::mapping_status
             ORDER BY s.confidence DESC, s.created_at LIMIT 500""", status)
        features = await conn.fetch("SELECT key, name FROM features WHERE status <> 'archived' ORDER BY name")
    return {"items": [row(r) for r in rows], "features": [row(f) for f in features]}


class Decision(BaseModel):
    decision: Literal["confirm", "reject", "reassign"]
    feature_key: str | None = None


@router.post("/triage/{suggestion_id}")
async def decide(suggestion_id: str, body: Decision, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        s = await conn.fetchrow("SELECT * FROM mapping_suggestions WHERE id = $1 FOR UPDATE", suggestion_id)
        if not s:
            raise HTTPException(404, "suggestion not found")
        if s["status"] != "pending":
            raise HTTPException(409, f"already {s['status']}")
        if body.decision == "reject":
            await conn.execute("UPDATE mapping_suggestions SET status = 'rejected', decided_at = now() WHERE id = $1",
                               s["id"])
            return {"status": "rejected"}
        if body.decision == "reassign":
            if not body.feature_key:
                raise HTTPException(422, "feature_key is required to reassign")
            fid = await conn.fetchval("SELECT id FROM features WHERE key = $1", body.feature_key)
            if not fid:
                raise HTTPException(404, "feature not found")
        else:
            fid = s["suggested_feature_id"]
            if fid is None:
                raise HTTPException(422, "nothing suggested: use reassign with a feature_key")
        st, sid = s["subject_type"], s["subject_id"]
        if st == "support_ticket":
            await conn.execute("""UPDATE support_tickets SET feature_id = $2, mapping_source = 'human',
                                  mapping_confidence = 1 WHERE id = $1""", sid, fid)
        elif st == "work_item":
            await conn.execute("DELETE FROM work_item_allocations WHERE work_item_id = $1", sid)
            await conn.execute("""INSERT INTO work_item_allocations (work_item_id, feature_id, org_id, share)
                                  VALUES ($1, $2, $3, 1)""", sid, fid, org)
        else:  # code_asset
            await conn.execute("""
                INSERT INTO asset_feature_mappings (asset_id, feature_id, org_id, source, confidence, status, decided_at)
                VALUES ($1, $2, $3, 'human', 1, 'confirmed', now())
                ON CONFLICT (asset_id, feature_id) DO UPDATE SET source = 'human', confidence = 1,
                    status = 'confirmed', decided_at = now()""", sid, fid, org)
        await conn.execute("""UPDATE mapping_suggestions SET status = 'confirmed', decided_feature_id = $2,
                              decided_at = now() WHERE id = $1""", s["id"], fid)
        return {"status": "confirmed", "feature_id": str(fid), "note": "takes effect on the next P&L run"}


@router.post("/manifest/validate")
async def validate_manifest(request: Request):
    """Body: the raw features.yaml (any content type)."""
    text = (await request.body()).decode("utf-8", errors="replace")
    try:
        m = parse_manifest(text)
    except ManifestError as e:
        return {"valid": False, "errors": e.errors}
    return {"valid": True, "features": [
        {"key": f.key, "name": f.name, "paths": len(f.paths), "routes": len(f.api_routes), "events": len(f.events),
         "shared_overhead": f.shared_overhead, "regulatory": f.regulatory} for f in m.features]}


@router.post("/jobs/compute-pnl")
async def compute(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        return await recompute(conn, org)


class GitHubSyncIn(BaseModel):
    repo: str
    default_team_key: str | None = None


@router.post("/integrations/github/sync", status_code=202)
async def github_sync(body: GitHubSyncIn, tasks: BackgroundTasks, org: str = Depends(current_org)):
    if not config.github_token:
        raise HTTPException(412, "set GITHUB_TOKEN (read-only: contents + pull requests) to enable GitHub sync")
    if body.repo.count("/") != 1:
        raise HTTPException(422, "repo must be owner/name")

    async def run() -> None:
        from ..ingest.github import GitHubClient, sync_repository
        manifest = parse_manifest(config.manifest_path.read_text())
        client = GitHubClient(config.github_token)
        try:
            async with org_tx(org) as conn:
                integ = await conn.fetchval("SELECT id FROM integrations WHERE kind = 'github' LIMIT 1")
                if integ is None:
                    integ = await conn.fetchval("""INSERT INTO integrations (org_id, kind, status, display_name)
                        VALUES ($1, 'github', 'active', 'GitHub') RETURNING id""", org)
                stats = await sync_repository(conn, org, str(integ), body.repo, client, manifest, body.default_team_key)
                log.info("github sync %s: %s", body.repo, stats)
            async with org_tx(org) as conn:
                await recompute(conn, org)
        except Exception:
            log.exception("github sync failed for %s", body.repo)
            async with org_tx(org) as conn:
                await conn.execute("""UPDATE integrations SET status = 'error', last_error = 'sync failed: see logs'
                                      WHERE kind = 'github'""")
        finally:
            await client.close()

    tasks.add_task(run)
    return {"status": "accepted", "repo": body.repo}


@router.get("/integrations")
async def integrations(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("""SELECT id, kind::text AS kind, status::text AS status, display_name, last_synced_at,
                                          last_error FROM integrations ORDER BY kind""")
    return [row(r) for r in rows]


@router.get("/settings")
async def settings(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        return row(await conn.fetchrow("SELECT * FROM org_settings"))
