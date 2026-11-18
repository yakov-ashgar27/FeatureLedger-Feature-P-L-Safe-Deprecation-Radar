"""Feature Graph: triage queue (human decisions), manifest history, and on-demand graph runs."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import org_tx
from ..graph.jobs import run_graph
from ..graph.manifest_store import active_manifest
from ..graph.reallocate import load_resolver, reallocate
from ..jobs.compute_pnl import recompute
from .deps import current_org, row

router = APIRouter(prefix="/api/v1", tags=["feature-graph"])


@router.get("/triage")
async def triage_queue(status: Literal["pending", "auto", "decided"] = "pending", tier: int | None = None,
                       org: str = Depends(current_org)):
    """pending: below 0.80, waiting for a person · auto: machine-confirmed (>= 0.80), undoable · decided: by a person."""
    cond = {"pending": "s.status = 'pending' AND s.decided_at IS NULL",
            "auto": "s.auto_confirmed AND s.decided_at IS NULL",
            "decided": "s.decided_at IS NOT NULL"}[status]
    async with org_tx(org) as conn:
        rows = await conn.fetch(f"""
            SELECT s.id, s.subject_type::text AS subject_type, s.subject_id, s.subject_label, s.confidence,
                   s.source::text AS source, s.rationale, s.status::text AS status, s.tier, s.evidence,
                   s.auto_confirmed, s.created_at, s.updated_at, s.decided_at,
                   f.key AS suggested_feature_key, f.name AS suggested_feature_name,
                   d.key AS decided_feature_key, d.name AS decided_feature_name
              FROM mapping_suggestions s
              LEFT JOIN features f ON f.id = s.suggested_feature_id
              LEFT JOIN features d ON d.id = s.decided_feature_id
             WHERE {cond} AND ($1::smallint IS NULL OR s.tier = $1)
             ORDER BY s.confidence DESC, s.created_at LIMIT 500""", tier)
        counts = await conn.fetchrow("""
            SELECT count(*) FILTER (WHERE status = 'pending' AND decided_at IS NULL) AS pending,
                   count(*) FILTER (WHERE auto_confirmed AND decided_at IS NULL) AS auto,
                   count(*) FILTER (WHERE decided_at IS NOT NULL) AS decided,
                   count(*) FILTER (WHERE tier = 2 AND decided_at IS NULL) AS tier2,
                   count(*) FILTER (WHERE tier = 3 AND decided_at IS NULL) AS tier3
              FROM mapping_suggestions""")
        features = await conn.fetch("""SELECT key, name FROM features WHERE status <> 'archived'
                                        AND NOT is_shared_overhead ORDER BY name""")
    return {"items": [row(r) for r in rows], "features": [row(f) for f in features], "counts": row(counts),
            "threshold": 0.80}


class Decision(BaseModel):
    decision: Literal["confirm", "reject", "reassign"]
    feature_key: str | None = None


async def _clear_machine(conn, subject_type: str, sid, label: str) -> None:
    """Remove what a machine applied for this subject (used by reject and before a human mapping)."""
    if subject_type == "support_ticket":
        await conn.execute("""UPDATE support_tickets SET feature_id = NULL, mapping_source = NULL,
                              mapping_confidence = NULL WHERE id = $1 AND mapping_source = 'ai'""", sid)
    elif subject_type == "work_item":
        await conn.execute("""DELETE FROM work_item_allocations a USING work_items w
                               WHERE a.work_item_id = w.id AND a.source = 'ai'
                                 AND (w.id = $1 OR $2 = ANY(w.linked_keys))""", sid, label)
    else:
        await conn.execute("DELETE FROM asset_feature_mappings WHERE asset_id = $1 AND source = 'heuristic'", sid)


@router.post("/triage/{suggestion_id}")
async def decide(suggestion_id: str, body: Decision, org: str = Depends(current_org)):
    """A person's decision. Writes a `human` mapping (which outranks manifest and machine) or a rejection
    that the tiers will never re-propose, then re-allocates PR effort."""
    async with org_tx(org) as conn:
        s = await conn.fetchrow("SELECT * FROM mapping_suggestions WHERE id = $1 FOR UPDATE", suggestion_id)
        if not s:
            raise HTTPException(404, "suggestion not found")
        if s["decided_at"] is not None:
            raise HTTPException(409, f"already decided ({s['status']})")
        st, sid, label = s["subject_type"], s["subject_id"], s["subject_label"]
        is_issue = st == "work_item" and await conn.fetchval(
            "SELECT item_type = 'issue' FROM work_items WHERE id = $1", sid)
        issue_key = await conn.fetchval("SELECT external_id FROM work_items WHERE id = $1", sid) if is_issue else None
        await _clear_machine(conn, st, sid, issue_key or "")
        if body.decision == "reject":
            await conn.execute("""UPDATE mapping_suggestions SET status = 'rejected', decided_at = now(),
                                  auto_confirmed = false, decided_feature_id = NULL WHERE id = $1""", s["id"])
            fid = None
        else:
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
            if st == "support_ticket":
                await conn.execute("""UPDATE support_tickets SET feature_id = $2, mapping_source = 'human',
                                      mapping_confidence = 1 WHERE id = $1""", sid, fid)
            elif st == "work_item":
                pr_ids = [sid] if not is_issue else [r["id"] for r in await conn.fetch(
                    "SELECT id FROM work_items WHERE item_type = 'pr' AND $1 = ANY(linked_keys)", issue_key)]
                await conn.execute("DELETE FROM work_item_allocations WHERE work_item_id = ANY($1::uuid[])", pr_ids)
                await conn.executemany("""INSERT INTO work_item_allocations (work_item_id, feature_id, org_id, share, source)
                                          VALUES ($1, $2, $3, 1, 'human')""", [(p, fid, org) for p in pr_ids])
            else:
                await conn.execute("""
                    INSERT INTO asset_feature_mappings (asset_id, feature_id, org_id, source, confidence, status, decided_at)
                    VALUES ($1, $2, $3, 'human', 1, 'confirmed', now())
                    ON CONFLICT (asset_id, feature_id) DO UPDATE SET source = 'human', confidence = 1,
                        status = 'confirmed', decided_at = now()""", sid, fid, org)
            await conn.execute("""UPDATE mapping_suggestions SET status = 'confirmed', decided_feature_id = $2,
                                  decided_at = now(), auto_confirmed = false WHERE id = $1""", s["id"], fid)
        moved = await reallocate(conn, org, await load_resolver(conn, await active_manifest(conn)))
    return {"status": "rejected" if fid is None else "confirmed", "feature_id": str(fid) if fid else None,
            "reallocated_prs": moved["reallocated"], "note": "recompute the P&L to see the effect"}


@router.post("/graph/run")
async def graph_run(recompute_pnl: bool = True, org: str = Depends(current_org)):
    """Re-run reallocation and Tier 2 now (Tier 3 runs inside syncs, where text is available)."""
    async with org_tx(org) as conn:
        out = await run_graph(conn, org)
        if recompute_pnl:
            out["pnl"] = await recompute(conn, org)
    return out


@router.get("/manifest/versions")
async def manifest_versions(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("""SELECT id, source, repo, path, commit_sha, status, errors, summary, received_at
                                     FROM manifest_versions ORDER BY received_at DESC, id DESC LIMIT 50""")
    return [row(r) for r in rows]
