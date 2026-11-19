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
                 window: int = Query(3, ge=1, le=12), include_archived: bool = False,
                 org: str = Depends(current_org)):
    """Feature P&L matrix. Money columns are monthly averages over the trailing `window` months.

    Archived (sunset) features keep their full monthly history but are hidden by default; pass
    include_archived=true to list them (flagged `archived: true`). Summary KPIs always describe the
    live portfolio, so the toggle never changes them."""
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
            "c_ai_tokens": round(avg("c_ai_tokens"), 2),
            "gross_margin_pct": round((avg("v_usage") - avg("c_ai_tokens") - avg("c_infra")) / max(avg("v_usage"), 1.0), 4),
            "margin_bleed": bool(cur["margin_bleed"]), "margin_bleed_reason": cur["margin_bleed_reason"],
            "ai_tier_breakdown": cur["ai_tier_breakdown"] or {},
            "value": round(value, 2), "net_roi": round(value - c_total, 2),
            "efficiency": round(value / max(c_total, 1.0), 3),
            "adoption": round(cur["active_accounts"] / cur["eligible_accounts"], 4) if cur["eligible_accounts"] else 0,
            "active_accounts": cur["active_accounts"], "eligible_accounts": cur["eligible_accounts"],
            "anchor_accounts": cur["anchor_accounts"], "revenue_at_risk_arr": cur["revenue_at_risk_arr"],
            "bug_tax_ratio": cur["bug_tax_ratio"], "tickets": round(avg("tickets"), 1),
            "months": n, "archived": cur["status"] == "archived",
        })
    out.sort(key=lambda x: x["net_roi"])
    from ..reports.confidence import confidence_by_feature
    async with org_tx(org) as conn:
        conf = await confidence_by_feature(conn, end)
    for x in out:
        c = conf.get(x["key"])
        x["confidence"] = {"level": c["level"], "score": c["score"], "reasons": c["reasons"]} if c else None
    archived = [x for x in out if x["archived"]]
    out = [x for x in out if not x["archived"]]
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
            "ai_token_spend_month": round(sum(x["c_ai_tokens"] for x in product), 2),
            "margin_bleed_count": sum(1 for x in product if x["margin_bleed"]),
            "margin_bleed_features": [x["key"] for x in product if x["margin_bleed"]],
            "untagged_infra_month": float(untagged),
            "eng_capacity_hours": plain(cap["cap"]), "unmapped_eng_hours": plain(cap["unmapped"]),
            "unattributed_eng_hours": plain(cap["unattributed"]),
        },
        "features": out + (archived if include_archived else []),
        "archived_count": len(archived),
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
        feats = await conn.fetch("""SELECT id, key, is_shared_overhead, is_regulatory, status::text AS status
                                    FROM features WHERE key = ANY($1)""",
                                 body.feature_keys)
        found = {r["key"]: r for r in feats}
        missing = set(body.feature_keys) - set(found)
        if missing:
            raise HTTPException(404, f"unknown features: {sorted(missing)}")
        gone = [k for k, r in found.items() if r["status"] == "archived"]
        if gone:
            raise HTTPException(422, f"already archived: {sorted(gone)}")
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
            c_ai_tokens=float(r["c_ai_tokens"]), v_usage=float(r["v_usage"])))
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


@router.get("/ai-margin")
async def ai_margin(months: int = Query(6, ge=1, le=24), org: str = Depends(current_org)):
    """Token-Bleed Radar: every feature with LLM spend, its gross margin trend, and token burn vs revenue by tier."""
    async with org_tx(org) as conn:
        rows = await conn.fetch("""
            SELECT f.key, f.name, f.tier_gate, p.period_month, p.v_usage, p.c_ai_tokens, p.c_infra, p.c_total,
                   p.gross_margin_pct, p.margin_bleed, p.margin_bleed_reason, p.ai_tier_breakdown,
                   p.active_accounts, p.eligible_accounts, p.classification::text AS classification
              FROM feature_pnl_monthly p JOIN features f ON f.id = p.feature_id
             WHERE p.feature_id IN (SELECT feature_id FROM feature_pnl_monthly WHERE c_ai_tokens > 0)
               AND f.status <> 'archived'
               AND p.period_month > (SELECT max(period_month) FROM feature_pnl_monthly) - make_interval(months => $1)
             ORDER BY f.key, p.period_month""", months)
        thresholds = await conn.fetchrow("""SELECT margin_bleed_min_adoption, margin_bleed_max_gross_margin
                                              FROM org_settings""")
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["key"], {"key": r["key"], "name": r["name"], "tier_gate": r["tier_gate"], "history": []})
        d["history"].append({"period": r["period_month"].isoformat()[:7], "v_usage": plain(r["v_usage"]),
                             "c_ai_tokens": plain(r["c_ai_tokens"]), "c_infra": plain(r["c_infra"]),
                             "gross_margin_pct": plain(r["gross_margin_pct"]), "margin_bleed": r["margin_bleed"],
                             "adoption": round(r["active_accounts"] / r["eligible_accounts"], 4)
                             if r["eligible_accounts"] else 0})
        d.update(classification=r["classification"], margin_bleed=r["margin_bleed"],
                 margin_bleed_reason=r["margin_bleed_reason"], tiers=r["ai_tier_breakdown"] or {})
    feats = sorted(out.values(), key=lambda d: (not d["margin_bleed"], -(d["history"][-1]["c_ai_tokens"] or 0)))
    return {"thresholds": {"min_adoption": plain(thresholds["margin_bleed_min_adoption"]) if thresholds else 0.15,
                           "max_gross_margin": plain(thresholds["margin_bleed_max_gross_margin"]) if thresholds else 0.2},
            "features": feats}


@router.get("/features/{key}/explain")
async def explain(key: str, period: str | None = None, org: str = Depends(current_org)):
    """Why this number: how each cost and value line is computed, from which data, and how sure it is."""
    from ..reports.confidence import assess, load_evidence
    p = parse_period(period)
    async with org_tx(org) as conn:
        ev = (await load_evidence(conn, p)).get(key)
        if ev is None:
            raise HTTPException(404, "no P&L row for this feature and month")
        s = await conn.fetchrow("""SELECT eng_hourly_rate_usd, support_hourly_rate_usd FROM org_settings""")
        row_ = await conn.fetchrow("""SELECT p.period_month, p.eng_hours, p.tickets, p.c_total, p.classification::text AS cls,
                                             p.classification_reason FROM feature_pnl_monthly p JOIN features f ON f.id = p.feature_id
                                       WHERE f.key = $1 AND p.period_month = coalesce($2::date,
                                             (SELECT max(period_month) FROM feature_pnl_monthly))""", key, p)
    c = assess(ev)
    eng_rate, sup_rate = plain(s["eng_hourly_rate_usd"]), plain(s["support_hourly_rate_usd"])
    lines = [
        {"line": "Engineering", "amount": plain(ev["c_eng"]),
         "how": f"{plain(row_['eng_hours']):,.0f} h x ${eng_rate:,.0f}/h. Hours = each merged PR's relative effort "
                "(size, files, review rounds, kind), scaled so every team's month adds up to its real capacity, "
                "split across features by the code each PR touched.",
         "measured": c["coverage"]["eng_declared_share"], "measured_label": "from declared mappings"},
        {"line": "Support", "amount": plain(ev["c_support"]),
         "how": f"{row_['tickets']} ticket(s) x handle time x ${sup_rate:,.0f}/h.",
         "measured": c["coverage"]["support_measured_share"], "measured_label": "with measured handle time"},
        {"line": "Cloud infra", "amount": plain(ev["c_infra"]),
         "how": "Cost lines tagged to the feature, plus its usage share of untagged spend.",
         "measured": c["coverage"]["infra_tagged_share"], "measured_label": "tagged directly"},
        {"line": "AI tokens", "amount": plain(ev["c_ai_tokens"]),
         "how": "LLM/AI usage cost lines tagged to the feature.", "measured": None, "measured_label": None},
        {"line": "Value (usage-weighted)", "amount": plain(ev["v_usage"]),
         "how": "Each paying account's MRR, split across the features it used this month in proportion to its "
                "usage events. A modelling choice: compare with the tier-gated figure.",
         "measured": None, "measured_label": None},
        {"line": "Value (tier-gated)", "amount": plain(ev["v_tier"]),
         "how": "MRR of the plan tiers that include the feature, blended by adoption.",
         "measured": None, "measured_label": None},
    ]
    return {"key": key, "period": row_["period_month"].isoformat()[:7], "classification": row_["cls"],
            "classification_reason": row_["classification_reason"], "lines": lines, "confidence": c}
