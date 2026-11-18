"""What-If Sunset Simulator.

For a set S of features over a trailing window W (default 3 months):
    reclaimed_hours/mo  = avg_W(eng_hours) * (1 - residual)          residual = 10% (on-call, migration tail)
    avoidable_cost/mo   = avg_W(C_eng) * (1 - residual) + avg_W(C_support) + avg_W(C_infra)
    expected_arr_loss   = sum_accounts ARR * max_f churn_risk(a,f) * (1 - migration_success)
                          (an account hit by two sunset features is counted once, at its highest risk)
    net_annual_impact   = 12 * avoidable_cost/mo - expected_arr_loss - one_off_migration_cost
    fte_reclaimed       = reclaimed_hours / (hours_per_engineer_month * allocatable_fraction)
Shared overhead is never "reclaimed": removing a feature does not remove auth/ or the database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .pnl import FeaturePnl
from .types import OrgSettings

RESIDUAL = 0.10


@dataclass(frozen=True)
class AccountExposure:
    account_id: str
    name: str
    arr_usd: float
    churn_risk: float
    feature_keys: tuple[str, ...]


def simulate(
    rows_by_feature: dict[str, list[FeaturePnl]],
    exposures: list[tuple[str, str, float, float, str]],  # (account_id, name, arr, churn_risk, feature_key)
    settings: OrgSettings,
    migration_success: float = 0.7,
    one_off_migration_cost: float = 0.0,
) -> dict:
    if not 0 <= migration_success <= 1:
        raise ValueError("migration_success must be within [0, 1]")
    per_feature = []
    hours = cost = 0.0
    for key, rows in rows_by_feature.items():
        n = max(len(rows), 1)
        h = sum(r.eng_hours for r in rows) / n * (1 - RESIDUAL)
        c = (sum(r.c_eng for r in rows) / n * (1 - RESIDUAL)
             + sum(r.c_support for r in rows) / n + sum(r.c_infra for r in rows) / n)
        v = sum(r.v_usage for r in rows) / n
        hours += h
        cost += c
        per_feature.append({"feature_key": key, "reclaimed_hours_month": round(h, 1),
                            "avoidable_cost_month": round(c, 2), "attributed_mrr": round(v, 2)})

    accts: dict[str, AccountExposure] = {}
    for aid, name, arr, risk, fkey in exposures:
        prev = accts.get(aid)
        if prev is None:
            accts[aid] = AccountExposure(aid, name, arr, risk, (fkey,))
        else:
            accts[aid] = AccountExposure(aid, name, arr, max(prev.churn_risk, risk),
                                         tuple(sorted(set(prev.feature_keys) | {fkey})))
    expected_loss = sum(a.arr_usd * a.churn_risk * (1 - migration_success) for a in accts.values())
    fte_hours = settings.hours_per_engineer_month * settings.allocatable_fraction
    return {
        "features": per_feature,
        "reclaimed_hours_month": round(hours, 1),
        "fte_reclaimed": round(hours / fte_hours, 2) if fte_hours else 0.0,
        "avoidable_cost_month": round(cost, 2),
        "avoidable_cost_year": round(cost * 12, 2),
        "accounts_affected": len(accts),
        "arr_touched": round(sum(a.arr_usd for a in accts.values()), 2),
        "expected_arr_loss": round(expected_loss, 2),
        "one_off_migration_cost": round(one_off_migration_cost, 2),
        "net_annual_impact": round(cost * 12 - expected_loss - one_off_migration_cost, 2),
        "migration_success_assumed": migration_success,
        "accounts": sorted(
            ({"account_id": a.account_id, "name": a.name, "arr_usd": a.arr_usd,
              "churn_risk": a.churn_risk, "features": list(a.feature_keys)} for a in accts.values()),
            key=lambda x: -x["arr_usd"]),
    }


def trailing_months(end: date, n: int) -> list[date]:
    out, y, m = [], end.year, end.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(out)
