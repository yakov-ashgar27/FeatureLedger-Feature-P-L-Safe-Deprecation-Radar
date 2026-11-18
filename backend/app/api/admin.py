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
        from ..graph.jobs import run_graph
        from ..graph.manifest_store import active_manifest
        from ..graph.semantic_classifier import TextVault
        from ..ingest.github import GitHubClient, sync_repository
        client = GitHubClient(config.github_token)
        vault = TextVault()
        try:
            async with org_tx(org) as conn:
                manifest = await active_manifest(conn)
                integ = await conn.fetchval("SELECT id FROM integrations WHERE kind = 'github' LIMIT 1")
                if integ is None:
                    integ = await conn.fetchval("""INSERT INTO integrations (org_id, kind, status, display_name)
                        VALUES ($1, 'github', 'active', 'GitHub') RETURNING id""", org)
                stats = await sync_repository(conn, org, str(integ), body.repo, client, manifest, body.default_team_key,
                                              vault)
                log.info("github sync %s: %s", body.repo, stats)
            async with org_tx(org) as conn:
                log.info("graph after github sync: %s", await run_graph(conn, org, vault))
                await recompute(conn, org)
        except Exception:
            log.exception("github sync failed for %s", body.repo)
            async with org_tx(org) as conn:
                await conn.execute("""UPDATE integrations SET status = 'error', last_error = 'sync failed: see logs'
                                      WHERE kind = 'github'""")
        finally:
            vault.clear()
            await client.close()

    tasks.add_task(run)
    return {"status": "accepted", "repo": body.repo}


@router.get("/settings")
async def settings(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        return row(await conn.fetchrow("SELECT * FROM org_settings"))
