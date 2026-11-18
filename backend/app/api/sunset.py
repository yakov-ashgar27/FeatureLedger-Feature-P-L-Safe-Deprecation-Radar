"""Safe Sunset pipeline endpoints. Every state change writes a hash-chained audit row."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..graph.manifest_store import active_manifest
from ..core.sunset import PlanView, SunsetState, TransitionError, TransitionResult, advance, cancel, rollback
from ..core.sunset_artifacts import DossierAccount, blast_radius, cleanup_pr_outline, impact_dossier
from ..db import org_tx
from .deps import current_org, row

router = APIRouter(prefix="/api/v1/sunset-plans", tags=["sunset"])

PLAN_SQL = """
    SELECT p.*, f.key AS feature_key, f.name AS feature_name, f.flag_key, f.is_regulatory, f.is_shared_overhead,
           f.metadata AS feature_metadata
      FROM sunset_plans p JOIN features f ON f.id = p.feature_id"""


def _view(p) -> PlanView:
    return PlanView(state=SunsetState(p["state"]), feature_is_regulatory=p["is_regulatory"], flag_key=p["flag_key"],
                    scream_test_days=p["scream_test_days"], state_entered_at=p["state_entered_at"],
                    canary_phase=p["canary_phase"], blast_radius=p["blast_radius"], impact_dossier=p["impact_dossier"],
                    canary=p["canary"], cleanup_pr=p["cleanup_pr"])


async def _audit(conn, org: str, plan_id, frm: str | None, to: str, action: str, payload: dict[str, Any]) -> None:
    await conn.execute("""INSERT INTO sunset_audit_logs (org_id, plan_id, from_state, to_state, action, payload)
                          VALUES ($1, $2, $3::sunset_state, $4::sunset_state, $5, $6)""",
                       org, plan_id, frm, to, action, payload)


async def _load(conn, plan_id: str, for_update: bool = False):
    p = await conn.fetchrow(PLAN_SQL + " WHERE p.id = $1" + (" FOR UPDATE OF p" if for_update else ""), plan_id)
    if not p:
        raise HTTPException(404, "sunset plan not found")
    return p


async def _apply(conn, org: str, p, res: TransitionResult, expected_version: int | None, note: dict) -> dict:
    if expected_version is not None and expected_version != p["version"]:
        raise HTTPException(409, f"plan changed (version {p['version']}); reload and retry")
    u = res.updates
    entered = datetime.now(timezone.utc) if res.to_state != res.from_state or res.action == "canary_promote" \
        else p["state_entered_at"]
    await conn.execute("""
        UPDATE sunset_plans SET state = $2::sunset_state, canary_phase = $3,
               blast_radius = coalesce($4, blast_radius), impact_dossier = coalesce($5, impact_dossier),
               canary = coalesce($6, canary), cleanup_pr = coalesce($7, cleanup_pr),
               state_entered_at = $8, updated_at = now(), version = version + 1
         WHERE id = $1""", p["id"], res.to_state.value,
        u["canary_phase"] if "canary_phase" in u else p["canary_phase"],
        u.get("blast_radius"), u.get("impact_dossier"), u.get("canary"), u.get("cleanup_pr"), entered)
    if res.to_state is SunsetState.ARCHIVED:
        await conn.execute("UPDATE features SET status = 'archived', updated_at = now() WHERE id = $1", p["feature_id"])
    elif res.to_state is SunsetState.CANCELLED:
        await conn.execute("UPDATE features SET status = 'active', updated_at = now() WHERE id = $1", p["feature_id"])
    elif res.to_state in (SunsetState.CANARY_DARK_LAUNCH, SunsetState.DEAD_CODE_PR_OPENED):
        await conn.execute("UPDATE features SET status = 'sunsetting' WHERE id = $1", p["feature_id"])
    await _audit(conn, org, p["id"], res.from_state.value, res.to_state.value, res.action,
                 {**note, **({"flag_action": u["flag_action"]} if "flag_action" in u else {})})
    return row(await _load(conn, p["id"]))


@router.get("")
async def list_plans(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        latest = await conn.fetchval("SELECT max(period_month) FROM feature_pnl_monthly")
        rows = await conn.fetch(PLAN_SQL + " ORDER BY p.created_at")
        metrics = {str(r["feature_id"]): row(r) for r in await conn.fetch(
            """SELECT feature_id, eng_hours, c_total, revenue_at_risk_arr, active_accounts FROM feature_pnl_monthly
                WHERE period_month = $1""", latest)}
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        d = row(r)
        d["days_in_state"] = (now - r["state_entered_at"]).days
        d["metrics"] = metrics.get(str(r["feature_id"]))
        for k in ("blast_radius", "impact_dossier", "cleanup_pr"):
            d[f"has_{k}"] = r[k] is not None
            d.pop(k, None)
        out.append(d)
    return out


class CreatePlan(BaseModel):
    feature_key: str
    reason: str = Field(min_length=10, max_length=2000)
    scream_test_days: int = Field(30, ge=7, le=180)


@router.post("", status_code=201)
async def create_plan(body: CreatePlan, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        f = await conn.fetchrow("SELECT * FROM features WHERE key = $1", body.feature_key)
        if not f:
            raise HTTPException(404, "feature not found")
        if f["is_regulatory"] or f["is_shared_overhead"]:
            raise HTTPException(422, "regulatory and shared-overhead features cannot be sunset")
        try:
            pid = await conn.fetchval("""
                INSERT INTO sunset_plans (org_id, feature_id, reason, scream_test_days)
                VALUES ($1, $2, $3, $4) RETURNING id""", org, f["id"], body.reason, body.scream_test_days)
        except Exception as e:  # unique live plan per feature
            if "sunset_plans_one_live_uidx" in str(e):
                raise HTTPException(409, "this feature already has a live sunset plan") from e
            raise
        await conn.execute("UPDATE features SET status = 'candidate_for_sunset' WHERE id = $1", f["id"])
        await _audit(conn, org, pid, None, "candidate_for_sunset", "transition", {"reason": body.reason})
        return row(await _load(conn, pid))


@router.get("/{plan_id}")
async def get_plan(plan_id: str, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id)
        log = await conn.fetch("""SELECT id, from_state::text, to_state::text, action, payload, created_at,
                                         encode(hash, 'hex') AS hash FROM sunset_audit_logs
                                   WHERE plan_id = $1 ORDER BY id""", plan_id)
    return {**row(p), "audit_log": [row(x) for x in log]}


@router.post("/{plan_id}/evidence/{kind}")
async def generate_evidence(plan_id: str, kind: Literal["blast_radius", "impact_dossier", "cleanup_pr"],
                            org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        manifest = await active_manifest(conn)
        p = await _load(conn, plan_id, for_update=True)
        spec = manifest.by_key().get(p["feature_key"])
        if spec is None:
            raise HTTPException(422, f"{p['feature_key']} is not in features.yaml")
        if kind in ("blast_radius", "cleanup_pr"):
            edges = [(r["from_path"], r["to_path"]) for r in await conn.fetch("SELECT from_path, to_path FROM import_edges")]
            br = blast_radius(spec, manifest, edges)
            if kind == "blast_radius":
                value = br
            else:
                files = {r[0] for r in await conn.fetch(
                    "SELECT DISTINCT jsonb_array_elements(path_stats)->>'path' FROM work_items")}
                files |= {e[1] for e in edges} | {e[0] for e in edges}
                value = cleanup_pr_outline(spec, p["blast_radius"] or br, sorted(files))
        else:
            latest = await conn.fetchval("SELECT max(period_month) FROM feature_pnl_monthly")
            rows = await conn.fetch("""
                SELECT a.id, a.name, a.plan_tier, a.renewal_date, a.csm_email, max(d.arr_usd) arr,
                       sum(d.feature_active_days) days, max(d.dependency_score) dep, max(d.churn_risk) risk
                  FROM account_dependencies d JOIN accounts a ON a.id = d.account_id
                 WHERE d.feature_id = $1 AND d.arr_usd > 0 AND d.period_month > ($2::date - interval '3 months')
                 GROUP BY a.id""", p["feature_id"], latest)
            repl_key = (p["feature_metadata"] or {}).get("replaced_by")
            repl = await conn.fetchval("SELECT name FROM features WHERE key = $1", repl_key) if repl_key else None
            value = impact_dossier(p["feature_name"], repl, [
                DossierAccount(str(r["id"]), r["name"], float(r["arr"]), r["plan_tier"], r["renewal_date"],
                               int(r["days"]), float(r["dep"]), float(r["risk"]), r["csm_email"]) for r in rows],
                sunset_date=date.today() + timedelta(days=60))
        await conn.execute(f"UPDATE sunset_plans SET {kind} = $2, updated_at = now(), version = version + 1 "
                           "WHERE id = $1", p["id"], value)
        await _audit(conn, org, p["id"], p["state"], p["state"], "note", {"evidence": kind})
    return value


class Ack(BaseModel):
    kind: Literal["inbound", "account"]
    ids: list[str] = Field(min_length=1)


@router.post("/{plan_id}/acknowledge")
async def acknowledge(plan_id: str, body: Ack, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        col, list_key, id_key = (("blast_radius", "inbound_references", "from_path") if body.kind == "inbound"
                                 else ("impact_dossier", "accounts", "account_id"))
        doc = p[col]
        if not doc:
            raise HTTPException(409, f"generate {col} first")
        wanted = set(body.ids)
        for item in doc[list_key]:
            if "*" in wanted or item[id_key] in wanted:
                item["acknowledged"] = True
        await conn.execute(f"UPDATE sunset_plans SET {col} = $2, version = version + 1 WHERE id = $1", p["id"], doc)
        await _audit(conn, org, p["id"], p["state"], p["state"], "note", {"acknowledged": body.kind, "ids": body.ids})
        return row(await _load(conn, plan_id))


class AdvanceIn(BaseModel):
    expected_version: int | None = None
    cs_signoff: bool = False
    pr_merged: bool = False


@router.post("/{plan_id}/advance")
async def advance_plan(plan_id: str, body: AdvanceIn, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        ctx: dict[str, Any] = {"blast_radius": p["blast_radius"], "impact_dossier": p["impact_dossier"],
                               "cleanup_pr": p["cleanup_pr"], "cs_signoff": body.cs_signoff,
                               "pr_merged": body.pr_merged}
        canary = p["canary"] or {}
        if canary.get("fully_off_at"):
            ctx["tickets_since_off"] = await conn.fetchval(
                "SELECT count(*) FROM support_tickets WHERE feature_id = $1 AND created_at >= $2",
                p["feature_id"], datetime.fromisoformat(canary["fully_off_at"]))
            # Phase 1: flag evaluations come from the LaunchDarkly/Statsig connector when installed
            ctx["flag_on_evaluations"] = int(canary.get("flag_on_evaluations", 0))
        try:
            res = advance(_view(p), ctx)
        except TransitionError as e:
            raise HTTPException(409, {"code": e.code, "message": str(e)}) from e
        return await _apply(conn, org, p, res, body.expected_version,
                            {"cs_signoff": body.cs_signoff, "pr_merged": body.pr_merged})


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/{plan_id}/rollback")
async def rollback_plan(plan_id: str, body: ReasonIn, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        try:
            res = rollback(_view(p), body.reason)
        except TransitionError as e:
            raise HTTPException(409, {"code": e.code, "message": str(e)}) from e
        return await _apply(conn, org, p, res, None, {"reason": body.reason})


@router.post("/{plan_id}/cancel")
async def cancel_plan(plan_id: str, body: ReasonIn, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        try:
            res = cancel(_view(p), body.reason)
        except TransitionError as e:
            raise HTTPException(409, {"code": e.code, "message": str(e)}) from e
        return await _apply(conn, org, p, res, None, {"reason": body.reason})
