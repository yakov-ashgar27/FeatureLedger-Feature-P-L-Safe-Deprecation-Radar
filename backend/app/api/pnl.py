"""Ledger, tear-sheet and what-if endpoints."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.manifest import glob_to_regex
from ..core.pnl import FeaturePnl
from ..core.simulate import simulate, trailing_months
from ..core.types import AttributionModel, FeatureClass
from ..db import org_tx
from ..jobs.compute_pnl import load_dataset
from .deps import current_org, parse_period, plain, row

router = APIRouter(prefix="/api/v1", tags=["pnl"])
Model = Literal["usage_weighted", "tier_gated", "critical_dependency"]
BUG_KINDS = ("bug", "hotfix", "incident", "revert")


async def _latest(conn) -> date:
    m = await conn.fetchval("SELECT max(period_month) FROM feature_pnl_monthly")
    if m is None:
        raise HTTPException(409, "no P&L computed yet: POST /api/v1/jobs/compute-pnl")
    return m


def _value(r: dict, model: str) -> float:
    if model == "tier_gated":
        return r["v_tier"]
    if model == "critical_dependency":
        return r["revenue_at_risk_arr"] / 12.0
    return r["v_usage"]


@router.get("/periods")
async def periods(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("SELECT DISTINCT period_month FROM feature_pnl_monthly ORDER BY 1 DESC")
    return [r[0].isoformat()[:7] for r in rows]


@router.get("/pnl")
async def ledger(period: str | None = None, model: Model = "usage_weighted",
                 view: Literal["contribution", "fully_loaded"] = "contribution",
                 window: int = Query(3, ge=1, le=12), org: str = Depends(current_org)):
    """Feature P&L matrix. Money columns are monthly averages over the trailing `window` months."""
    async with org_tx(org) as conn:
        end = parse_period(period) or await _latest(conn)
        months = trailing_months(end, window)
        rows = await conn.fetch("""
            SELECT p.*, f.key, f.name, f.status::text AS status, f.tier_gate, f.flag_key, f.is_shared_overhead,
                   f.is_regulatory, t.name AS team, sp.state::text AS sunset_state
              FROM feature_pnl_monthly p JOIN features f ON f.id = p.feature_id
              LEFT JOIN teams t ON t.id = f.owner_team_id
              LEFT JOIN sunset_plans sp ON sp.feature_id = f.id AND sp.state NOT IN ('archived', 'cancelled')
             WHERE p.period_month = ANY($1::date[]) ORDER BY p.period_month""", months)
        untagged = await conn.fetchval("""SELECT coalesce(sum(amount_usd), 0) FROM cost_events
            WHERE feature_id IS NULL AND period_month = $1""", end)
        cap = await conn.fetchrow("""SELECT sum(capacity_hours) cap, sum(unmapped_hours) unmapped,
            sum(unattributed_hours) unattributed, sum(overhead_hours) overhead
            FROM eng_capacity_monthly WHERE period_month = $1""", end)
    by_feature: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_feature[str(r["feature_id"])].append(row(r))
    out = []
    for fid, rs in by_feature.items():
        cur = rs[-1]
        n = len(rs)
        avg = lambda k: sum(x[k] for x in rs) / n  # noqa: E731
        c_total = avg("c_total") + (avg("c_overhead_alloc") if view == "fully_loaded" else 0.0)
        value = sum(_value(x, model) for x in rs) / n
        out.append({
            "feature_id": fid, "key": cur["key"], "name": cur["name"], "team": cur["team"],
            "status": cur["status"], "sunset_state": cur["sunset_state"], "tier_gate": cur["tier_gate"],
            "flag_key": cur["flag_key"], "is_shared_overhead": cur["is_shared_overhead"],
            "is_regulatory": cur["is_regulatory"],
            "classification": cur["classification"], "classification_reason": cur["classification_reason"],
            "eng_hours": round(avg("eng_hours"), 1), "c_eng": round(avg("c_eng"), 2),
            "c_support": round(avg("c_support"), 2), "c_infra": round(avg("c_infra"), 2),
            "c_overhead_alloc": round(avg("c_overhead_alloc"), 2), "c_total": round(c_total, 2),
            "value": round(value, 2), "net_roi": round(value - c_total, 2),
            "efficiency": round(value / max(c_total, 1.0), 3),
            "adoption": round(cur["active_accounts"] / cur["eligible_accounts"], 4) if cur["eligible_accounts"] else 0,
            "active_accounts": cur["active_accounts"], "eligible_accounts": cur["eligible_accounts"],
            "anchor_accounts": cur["anchor_accounts"], "revenue_at_risk_arr": cur["revenue_at_risk_arr"],
            "bug_tax_ratio": cur["bug_tax_ratio"], "tickets": round(avg("tickets"), 1),
            "months": n,
        })
    out.sort(key=lambda x: x["net_roi"])
    product = [x for x in out if not x["is_shared_overhead"]]
    zombies = [x for x in product if x["classification"] == FeatureClass.ZOMBIE.value]
    return {
        "period": end.isoformat()[:7], "window": window, "model": model, "view": view,
        "summary": {
            "features": len(product),
            "monthly_cost": round(sum(x["c_total"] for x in product), 2),
            "monthly_value": round(sum(x["value"] for x in product), 2) if model != "critical_dependency" else None,
            "platform_overhead": round(sum(x["c_total"] for x in out if x["is_shared_overhead"]), 2),
            "zombie_count": len(zombies),
            "zombie_tax_month": round(sum(x["c_total"] for x in zombies), 2),
            "zombie_eng_hours_month": round(sum(x["eng_hours"] for x in zombies), 1),
            "untagged_infra_month": float(untagged),
            "eng_capacity_hours": plain(cap["cap"]), "unmapped_eng_hours": plain(cap["unmapped"]),
            "unattributed_eng_hours": plain(cap["unattributed"]),
        },
        "features": out,
    }


@router.get("/features/{key}")
async def tear_sheet(key: str, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        f = await conn.fetchrow("""SELECT f.*, t.name AS team, t.headcount FROM features f
                                   LEFT JOIN teams t ON t.id = f.owner_team_id WHERE f.key = $1""", key)
        if not f:
            raise HTTPException(404, f"feature {key!r} not found")
        end = await _latest(conn)
        history = await conn.fetch("""SELECT * FROM feature_pnl_monthly WHERE feature_id = $1
                                      ORDER BY period_month""", f["id"])
        accounts = await conn.fetch("""
            SELECT a.id, a.name, a.plan_tier, a.health::text AS health, d.arr_usd, d.renewal_date,
                   d.feature_active_days, d.account_active_days, d.dependency_score, d.churn_risk, d.attributed_mrr
              FROM account_dependencies d JOIN accounts a ON a.id = d.account_id
             WHERE d.feature_id = $1 AND d.period_month = $2 AND d.arr_usd > 0
             ORDER BY d.arr_usd DESC, d.dependency_score DESC LIMIT 100""", f["id"], end)
        globs = [r["identifier"] for r in await conn.fetch("""
            SELECT a.identifier FROM code_assets a JOIN asset_feature_mappings m ON m.asset_id = a.id
             WHERE m.feature_id = $1 AND m.status = 'confirmed' AND a.asset_type = 'path_glob'""", f["id"])]
        assets = await conn.fetch("""
            SELECT a.asset_type::text AS asset_type, a.identifier, m.source::text AS source, m.confidence,
                   m.status::text AS status
              FROM code_assets a JOIN asset_feature_mappings m ON m.asset_id = a.id
             WHERE m.feature_id = $1 ORDER BY a.asset_type, a.identifier""", f["id"])
        bug_prs = await conn.fetch("""
            SELECT w.path_stats FROM work_items w JOIN work_item_allocations al ON al.work_item_id = w.id
             WHERE al.feature_id = $1 AND w.kind::text = ANY($2::text[])
               AND w.period_month > ($3::date - interval '6 months')""", f["id"], list(BUG_KINDS), end)
        plan = await conn.fetchrow("""SELECT id, state::text AS state FROM sunset_plans WHERE feature_id = $1
                                      AND state NOT IN ('archived', 'cancelled')""", f["id"])
    pats = [glob_to_regex(g) for g in globs]
    hot: dict[str, dict] = {}
    for r in bug_prs:
        for p in r["path_stats"] or []:
            if any(rx.match(p["path"]) for rx in pats):
                h = hot.setdefault(p["path"], {"path": p["path"], "bug_prs": 0, "churn": 0})
                h["bug_prs"] += 1
                h["churn"] += int(p.get("additions", 0)) + int(p.get("deletions", 0))
    team_visible = (f["headcount"] or 0) >= 3
    return {
        "feature": {**row(f), "team": f["team"] if team_visible else "(rolled up: team below display minimum)"},
        "history": [row(h) for h in history],
        "accounts": [row(a) for a in accounts],
        "top_bug_files": sorted(hot.values(), key=lambda x: (-x["bug_prs"], -x["churn"]))[:10],
        "assets": [row(a) for a in assets],
        "sunset_plan": row(plan) if plan else None,
    }


class SimulateIn(BaseModel):
    feature_keys: list[str] = Field(min_length=1, max_length=25)
    period: str | None = None
    window: int = Field(3, ge=1, le=12)
    migration_success: float = Field(0.7, ge=0, le=1)
    one_off_migration_cost: float = Field(0, ge=0)


@router.post("/simulate")
async def what_if(body: SimulateIn, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        end = parse_period(body.period) or await _latest(conn)
        months = trailing_months(end, body.window)
        feats = await conn.fetch("SELECT id, key, is_shared_overhead, is_regulatory FROM features WHERE key = ANY($1)",
                                 body.feature_keys)
        found = {r["key"]: r for r in feats}
        missing = set(body.feature_keys) - set(found)
        if missing:
            raise HTTPException(404, f"unknown features: {sorted(missing)}")
        blocked = [k for k, r in found.items() if r["is_shared_overhead"] or r["is_regulatory"]]
        if blocked:
            raise HTTPException(422, f"cannot simulate sunsetting platform/regulatory features: {blocked}")
        ids = [r["id"] for r in feats]
        rows = await conn.fetch("""SELECT p.*, f.key FROM feature_pnl_monthly p JOIN features f ON f.id = p.feature_id
                                   WHERE p.feature_id = ANY($1) AND p.period_month = ANY($2::date[])""", ids, months)
        exp = await conn.fetch("""
            SELECT d.account_id, a.name, max(d.arr_usd) arr, max(d.churn_risk) risk, f.key
              FROM account_dependencies d JOIN accounts a ON a.id = d.account_id JOIN features f ON f.id = d.feature_id
             WHERE d.feature_id = ANY($1) AND d.period_month = ANY($2::date[]) AND d.arr_usd > 0
             GROUP BY d.account_id, a.name, f.key""", ids, months)
        ds = await load_dataset(conn, [end])
    by_key: dict[str, list[FeaturePnl]] = defaultdict(list)
    for r in rows:
        by_key[r["key"]].append(FeaturePnl(
            str(r["feature_id"]), r["key"], r["period_month"], eng_hours=float(r["eng_hours"]),
            c_eng=float(r["c_eng"]), c_support=float(r["c_support"]), c_infra=float(r["c_infra"]),
            v_usage=float(r["v_usage"])))
    result = simulate(by_key, [(str(e["account_id"]), e["name"], float(e["arr"]), float(e["risk"]), e["key"])
                               for e in exp], ds.settings, body.migration_success, body.one_off_migration_cost)
    result["period"] = end.isoformat()[:7]
    result["window"] = body.window
    return result


@router.get("/teams")
async def teams(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        end = await _latest(conn)
        rows = await conn.fetch("""
            SELECT t.key, t.name, t.headcount, c.capacity_hours, c.calibration_factor, c.feature_hours,
                   c.overhead_hours, c.unmapped_hours, c.unattributed_hours
              FROM teams t LEFT JOIN eng_capacity_monthly c ON c.team_id = t.id AND c.period_month = $1
             ORDER BY t.name""", end)
    return [row(r) for r in rows]


@router.get("/models")
async def models():
    return [m.value for m in AttributionModel]
