"""P&L job: load one org's facts -> pure engine -> write ledger tables (idempotent per month set)."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

import asyncpg

from ..core.pnl import MODEL_VERSION, PnlResult, compute_pnl
from ..core.types import (Account, CostLine, Dataset, Feature, OrgSettings, Team, Ticket, Usage, WorkItem,
                          WorkKind)

log = logging.getLogger("featureledger.pnl")


def _f(v) -> float:
    return float(v) if isinstance(v, Decimal) else (0.0 if v is None else float(v))


async def available_months(conn: asyncpg.Connection) -> list[date]:
    rows = await conn.fetch("""SELECT DISTINCT period_month FROM account_snapshots
                               UNION SELECT DISTINCT period_month FROM usage_signals
                               UNION SELECT DISTINCT period_month FROM work_items WHERE merged_at IS NOT NULL
                               ORDER BY 1""")
    return [r[0] for r in rows]


async def load_dataset(conn: asyncpg.Connection, months: list[date]) -> Dataset:
    s = await conn.fetchrow("SELECT * FROM org_settings")
    settings = OrgSettings(
        eng_hourly_rate_usd=_f(s["eng_hourly_rate_usd"]), support_hourly_rate_usd=_f(s["support_hourly_rate_usd"]),
        hours_per_engineer_month=_f(s["hours_per_engineer_month"]), allocatable_fraction=_f(s["allocatable_fraction"]),
        calibration_clamp_max=_f(s["calibration_clamp_max"]), critical_concentration=_f(s["critical_concentration"]),
        anchor_min_arr_usd=_f(s["anchor_min_arr_usd"]),
        tier_uplift_share={k: float(v) for k, v in (s["tier_uplift_share"] or {}).items()},
    ) if s else OrgSettings()
    teams = [Team(str(r["id"]), r["key"], r["name"], r["headcount"])
             for r in await conn.fetch("SELECT id, key, name, headcount FROM teams")]
    features = [Feature(
        id=str(r["id"]), key=r["key"], name=r["name"],
        owner_team_id=str(r["owner_team_id"]) if r["owner_team_id"] else None, tier_gate=r["tier_gate"],
        is_shared_overhead=r["is_shared_overhead"], allocation_rule=r["allocation_rule"],
        allocation_weights=r["allocation_weights"], is_regulatory=r["is_regulatory"], launched_at=r["launched_at"],
        flag_key=r["flag_key"]) for r in await conn.fetch(
            "SELECT * FROM features WHERE status <> 'archived' ORDER BY key")]
    work_items = [WorkItem(
        id=str(r["id"]), team_id=str(r["team_id"]), period_month=r["period_month"], kind=WorkKind(r["kind"]),
        priority=r["priority"], additions=r["additions"], deletions=r["deletions"],
        files_changed=r["files_changed"], review_rounds=r["review_rounds"],
        incident_response_hours=_f(r["incident_response_hours"]), is_sweeping=r["is_sweeping"],
        allocations={str(k): float(v) for k, v in (r["alloc"] or {}).items()},
    ) for r in await conn.fetch("""
        SELECT w.*, (SELECT jsonb_object_agg(a.feature_id, a.share) FROM work_item_allocations a
                      WHERE a.work_item_id = w.id) AS alloc
          FROM work_items w
         WHERE w.period_month = ANY($1::date[]) AND w.team_id IS NOT NULL
           AND (w.item_type = 'pr' AND w.merged_at IS NOT NULL)""", months)]
    accounts_by_month: dict[date, list[Account]] = defaultdict(list)
    for r in await conn.fetch("""
        SELECT s.period_month, a.id, a.name, s.plan_tier, s.arr_usd, a.renewal_date, s.health
          FROM account_snapshots s JOIN accounts a ON a.id = s.account_id
         WHERE s.period_month = ANY($1::date[])""", months):
        accounts_by_month[r["period_month"]].append(Account(
            str(r["id"]), r["name"], r["plan_tier"], _f(r["arr_usd"]), r["renewal_date"], r["health"]))
    usage = [Usage(str(r["account_id"]), str(r["feature_id"]), r["period_month"], r["events"], r["active_days"],
                   r["active_users"]) for r in await conn.fetch(
        "SELECT * FROM usage_signals WHERE period_month = ANY($1::date[])", months)]
    act = {(r["period_month"], str(r["account_id"])): r["active_days"] for r in await conn.fetch(
        "SELECT * FROM account_activity_monthly WHERE period_month = ANY($1::date[])", months)}
    tickets = [Ticket(str(r["id"]), str(r["feature_id"]), str(r["account_id"]) if r["account_id"] else None,
                      r["period_month"], r["handle_minutes"]) for r in await conn.fetch(
        """SELECT id, feature_id, account_id, period_month, handle_minutes FROM support_tickets
            WHERE feature_id IS NOT NULL AND period_month = ANY($1::date[])""", months)]
    costs = [CostLine(str(r["feature_id"]) if r["feature_id"] else None, r["period_month"], r["cost_type"],
                      _f(r["amount_usd"])) for r in await conn.fetch(
        """SELECT feature_id, period_month, cost_type::text, amount_usd FROM cost_events
            WHERE cost_type IN ('infra', 'llm') AND period_month = ANY($1::date[])""", months)]
    return Dataset(settings, teams, features, work_items, dict(accounts_by_month), usage, act, tickets, costs)


async def write_result(conn: asyncpg.Connection, org_id: str, res: PnlResult, ds: Dataset) -> None:
    months = res.months
    for table in ("feature_pnl_monthly", "account_dependencies", "eng_capacity_monthly"):
        await conn.execute(f"DELETE FROM {table} WHERE period_month = ANY($1::date[])", months)
    await conn.execute("""DELETE FROM cost_events WHERE source = 'featureledger.model'
                          AND period_month = ANY($1::date[])""", months)
    await conn.executemany("""
        INSERT INTO feature_pnl_monthly (org_id, feature_id, period_month, eng_hours, bug_hours, c_eng, c_support,
            c_infra, c_total, c_overhead_alloc, v_usage, v_tier, revenue_at_risk_arr, tickets, active_accounts,
            eligible_accounts, anchor_accounts, bug_tax_ratio, classification, classification_reason, model_version)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19::feature_class,$20,$21)""", [
        (org_id, r.feature_id, m, round(r.eng_hours, 2), round(r.bug_hours, 2), round(r.c_eng, 2),
         round(r.c_support, 2), round(r.c_infra, 2), round(r.c_total, 2), round(r.c_overhead_alloc, 2),
         round(r.v_usage, 2), round(r.v_tier, 2), round(r.revenue_at_risk_arr, 2), r.tickets, r.active_accounts,
         r.eligible_accounts, r.anchor_accounts, round(min(r.bug_tax_ratio, 1.0), 4), r.classification.value,
         r.classification_reason, MODEL_VERSION)
        for (m, _), r in res.rows.items()])
    model_costs = []
    for (m, _), r in res.rows.items():
        if r.c_eng:
            model_costs.append((org_id, m, r.feature_id, "eng", round(r.c_eng, 2), round(r.eng_hours, 2), "hours"))
        if r.c_support:
            model_costs.append((org_id, m, r.feature_id, "support", round(r.c_support, 2), r.tickets, "tickets"))
    await conn.executemany("""
        INSERT INTO cost_events (org_id, period_month, feature_id, cost_type, source, amount_usd, quantity, unit)
        VALUES ($1, $2, $3, $4::cost_type, 'featureledger.model', $5, $6, $7)""", model_costs)
    await conn.executemany("""
        INSERT INTO eng_capacity_monthly (org_id, team_id, period_month, capacity_hours, raw_hours,
            calibration_factor, feature_hours, overhead_hours, unmapped_hours, unattributed_hours)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""", [
        (org_id, c.team_id, m, round(c.capacity_hours, 2), round(c.raw_hours, 2), round(c.factor, 4),
         round(c.feature_hours, 2), round(c.overhead_hours, 2), round(c.unmapped_hours, 2),
         round(c.unattributed_hours, 2)) for (m, _), c in res.calibration.items()])
    deps = []
    for m, att in res.attribution.items():
        for v in att.per_account:
            deps.append((org_id, v.feature_id, v.account_id, m, v.arr_usd, v.renewal_date, v.feature_active_days,
                         v.account_active_days, min(v.concentration, 1.0), v.churn_risk, v.attributed_mrr))
    await conn.executemany("""
        INSERT INTO account_dependencies (org_id, feature_id, account_id, period_month, arr_usd, renewal_date,
            feature_active_days, account_active_days, dependency_score, churn_risk, attributed_mrr)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""", deps)


async def recompute(conn: asyncpg.Connection, org_id: str, months: list[date] | None = None) -> dict:
    """Run inside an org-scoped transaction. Serialized per org: concurrent recomputes (worker + API) queue
    on a transaction-level advisory lock instead of colliding on the ledger's primary keys."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('featureledger:pnl:' || $1))", org_id)
    months = months or await available_months(conn)
    if not months:
        return {"months": 0}
    ds = await load_dataset(conn, months)
    res = compute_pnl(ds, months)
    await write_result(conn, org_id, res, ds)
    last = months[-1]
    counts: dict[str, int] = defaultdict(int)
    for r in res.month_rows(last):
        counts[r.classification.value] += 1
    return {"months": len(months), "features": len(ds.features), "work_items": len(ds.work_items),
            "latest": last.isoformat(), "classes": dict(counts), "model_version": MODEL_VERSION}


async def recompute_org(org_id: str, dsn: str | None = None) -> dict:
    from ..config import config
    from ..db import _init_conn
    conn = await asyncpg.connect(dsn or config.database_url, server_settings={"jit": "off"})
    try:
        await _init_conn(conn)
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.org_id', $1, true)", org_id)
            return await recompute(conn, org_id)
    finally:
        await conn.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(recompute_org(sys.argv[1])))
