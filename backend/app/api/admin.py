"""Feature Graph triage, manifest validation, jobs and connector triggers."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.session import Principal, require
from ..config import config
from ..core.manifest import ManifestError, parse_manifest
from ..db import org_tx
from ..jobs.compute_pnl import recompute
from .deps import current_org, org_for, row

router = APIRouter(prefix="/api/v1", tags=["admin"])
log = logging.getLogger("featureledger.api")


@router.post("/manifest/validate")
async def validate_manifest(request: Request, org: str = Depends(current_org)):
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
async def compute(org: str = Depends(org_for("pnl:recompute"))):
    async with org_tx(org) as conn:
        return await recompute(conn, org)


class GitHubSyncIn(BaseModel):
    repo: str
    default_team_key: str | None = None


@router.post("/integrations/github/sync", status_code=202)
async def github_sync(body: GitHubSyncIn, tasks: BackgroundTasks, org: str = Depends(org_for("integrations:manage"))):
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


class SettingsIn(BaseModel):
    """org_settings an admin may change. Omitted fields stay as they are."""
    eng_hourly_rate_usd: float | None = Field(None, gt=0, le=2000)
    support_hourly_rate_usd: float | None = Field(None, gt=0, le=2000)
    hours_per_engineer_month: float | None = Field(None, ge=40, le=220)
    allocatable_fraction: float | None = Field(None, ge=0.1, le=1)
    calibration_clamp_max: float | None = Field(None, ge=1, le=20)
    default_attribution_model: Literal["usage_weighted", "tier_gated", "critical_dependency"] | None = None
    critical_concentration: float | None = Field(None, ge=0.05, le=1)
    anchor_min_arr_usd: float | None = Field(None, ge=0)
    mapping_auto_confirm: float | None = Field(None, ge=0.5, le=1)
    min_team_size_for_display: int | None = Field(None, ge=1, le=50)


@router.put("/settings")
async def update_settings(body: SettingsIn, recompute_pnl: bool = True, who: Principal = Depends(require("settings:write"))):
    from decimal import Decimal
    changes = {k: Decimal(str(v)) if isinstance(v, float) else v for k, v in body.model_dump(exclude_none=True).items()}
    if not changes:
        raise HTTPException(422, "nothing to change")
    sets = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(changes))
    async with org_tx(who.org_id) as conn:
        before = row(await conn.fetchrow("SELECT * FROM org_settings"))
        await conn.execute(f"UPDATE org_settings SET {sets}, updated_at = now()", *changes.values())
        after = row(await conn.fetchrow("SELECT * FROM org_settings"))
        log.info("org_settings changed by %s (%s): %s", who.email, who.role.value,
                 {k: (before[k], after[k]) for k in changes})
        pnl = await recompute(conn, who.org_id) if recompute_pnl else None
    return {"settings": after, "changed": sorted(changes), "pnl": pnl}
