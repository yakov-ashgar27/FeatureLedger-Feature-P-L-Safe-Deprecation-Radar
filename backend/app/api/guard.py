"""Zombie-Guard reads: what the bot would say about a set of paths, and the comments it has posted."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..db import org_tx
from ..guard.zombie_guard import preview
from .deps import current_org, row

router = APIRouter(prefix="/api/v1/zombie-guard", tags=["zombie-guard"])


class PreviewIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=3000)
    head_sha: str | None = None


@router.post("/preview")
async def guard_preview(body: PreviewIn, org: str = Depends(current_org)):
    """Dry run: the comment Zombie-Guard would post on a PR changing these paths. Nothing is sent."""
    async with org_tx(org) as conn:
        return await preview(conn, body.paths, body.head_sha)


@router.get("/comments")
async def guard_comments(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("""SELECT repo, pr_number, provider, comment_id, head_sha, features, flagged, updates,
                                          body, created_at, updated_at
                                     FROM pr_guard_comments ORDER BY updated_at DESC LIMIT 50""")
    return [row(r) for r in rows]
