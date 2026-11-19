"""Executive Monthly Digest (Slack Block Kit) and the print-ready Executive Brief."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth.session import Principal, require
from ..db import org_is_demo, org_tx
from ..reports.digest import build_digest, post_to_slack, render_html, slack_blocks
from .deps import current_org, parse_period, row

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


def _period(p: str | None) -> date:
    d = parse_period(p)
    if d:
        return d
    now = datetime.now(timezone.utc)
    return date(now.year, now.month, 1)   # manual default: this month so far


@router.get("/executive-digest")
async def digest_preview(period: str | None = None, org: str = Depends(current_org)):
    """Build the digest without storing or sending it (the Slack payload is included for preview)."""
    async with org_tx(org) as conn:
        d = await build_digest(conn, _period(period))
    return {"summary": d, "slack": slack_blocks(d)}


class DigestIn(BaseModel):
    period: str | None = Field(None, pattern=r"^\d{4}-\d{2}$")
    post_to_slack: bool = True


@router.post("/executive-digest", status_code=201)
async def digest_send(body: DigestIn, who: Principal = Depends(require("reports:send"))):
    """Build, store and (unless SANDBOX_MODE / demo: dry run) post the digest to SLACK_WEBHOOK_URL."""
    async with org_tx(who.org_id) as conn:
        d = await build_digest(conn, _period(body.period))
        blocks = slack_blocks(d)
        status, err = (await post_to_slack(blocks, demo=await org_is_demo(conn))) if body.post_to_slack \
            else ("skipped", "post_to_slack=false")
        r = await conn.fetchrow("""
            INSERT INTO executive_digests (org_id, period_month, trigger, summary, slack_blocks, slack_status,
                                           slack_error, created_by)
            VALUES ($1, $2, 'manual', $3, $4, $5, $6, $7::uuid) RETURNING id, created_at""",
            who.org_id, _period(body.period), d, blocks, status, err, who.user_id)
    return {"id": str(r["id"]), "slack_status": status, "slack_error": err, "summary": d, "slack": blocks}


@router.get("/executive-digests")
async def digest_history(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("""SELECT id, period_month, trigger, slack_status, slack_error, created_at,
                                          summary->'zombie_tax'->'reclaimed_this_month' AS reclaimed
                                     FROM executive_digests ORDER BY created_at DESC LIMIT 24""")
    return [row(r) for r in rows]


@router.get("/executive-brief.html", response_class=HTMLResponse)
async def brief_html(period: str | None = None, org: str = Depends(current_org)):
    """Self-contained, print-ready brief: open it and use the browser's "Save as PDF"."""
    async with org_tx(org) as conn:
        d = await build_digest(conn, _period(period))
    if d["ledger_period"] is None:
        raise HTTPException(409, "no P&L computed yet")
    return HTMLResponse(render_html(d))
