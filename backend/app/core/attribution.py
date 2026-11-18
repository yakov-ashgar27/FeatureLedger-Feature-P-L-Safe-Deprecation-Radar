"""Value attribution: three models, two lenses.

Additive lens (sums to MRR, goes into the ledger):
  engagement  e(a,f) = active_days(a,f) * ln(1 + active_users(a,f))
      Active days, not raw events: a chatty autocomplete event must not outweigh a weekly export.
  usage_weighted   V(f) = sum_a  R(a) * e(a,f) / sum_g e(a,g)          (g over product features)
  tier_gated       for an account on tier T with uplift share u_T:
                   V(f) = sum_a [ u_T * R(a) * e(a,f) / sum_{g gated<=T} e(a,g) ]   if f is gated to T
                        + (1 - u_T) * R(a) * e(a,f) / sum_g e(a,g)
                   If the account used no gated feature, the uplift falls back to usage-weighted.

Counterfactual lens (NOT additive, guards decisions):
  concentration    c(a,f) = active_days(a,f) / distinct_active_days(a)
  churn risk       risk(a,f) = 0                                     if c < 0.10
                             = min(1, c / C*) * proximity * health   otherwise
                   proximity: renewal <= 90d -> 1.0, <= 180d -> 0.8, later/unknown -> 0.6
                   health:    red 1.0, yellow 0.9, green 0.7
  revenue at risk  RaR(f) = sum_a ARR(a) * risk(a,f)
  anchor account   c(a,f) >= C* and ARR(a) >= anchor_min_arr
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .types import Account, Feature, OrgSettings, Usage

TIER_RANK = {"free": 0, "starter": 1, "pro": 2, "enterprise": 3}
CONCENTRATION_FLOOR = 0.10
HEALTH_FACTOR = {"red": 1.0, "yellow": 0.9, "green": 0.7}


def engagement(active_days: int, active_users: int) -> float:
    if active_days <= 0 or active_users <= 0:
        return 0.0
    return active_days * math.log1p(active_users)


def renewal_proximity(renewal: date | None, as_of: date) -> float:
    if renewal is None:
        return 0.6
    days = (renewal - as_of).days
    if days <= 90:
        return 1.0
    if days <= 180:
        return 0.8
    return 0.6


def churn_risk(concentration: float, renewal: date | None, health: str, as_of: date,
               threshold: float) -> float:
    if concentration < CONCENTRATION_FLOOR:
        return 0.0
    r = min(1.0, concentration / threshold) * renewal_proximity(renewal, as_of) * HEALTH_FACTOR.get(health, 0.7)
    return round(min(max(r, 0.0), 1.0), 4)


@dataclass
class AccountFeatureValue:
    account_id: str
    feature_id: str
    feature_active_days: int
    account_active_days: int
    concentration: float
    churn_risk: float
    attributed_mrr: float
    arr_usd: float
    renewal_date: date | None


@dataclass
class MonthAttribution:
    v_usage: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    v_tier: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    revenue_at_risk: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    active_accounts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    anchor_accounts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # adoption denominator: paying accounts whose tier can use the feature (Enterprise-only features
    # are measured against Enterprise accounts, otherwise they would always look "niche")
    eligible_accounts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    per_account: list[AccountFeatureValue] = field(default_factory=list)
    unattributed_mrr: float = 0.0
    paying_accounts: int = 0
    total_mrr: float = 0.0


def attribute_month(
    month: date,
    accounts: list[Account],
    usage: list[Usage],
    account_active_days: dict[tuple[date, str], int],
    features: list[Feature],
    settings: OrgSettings,
) -> MonthAttribution:
    product = {f.id: f for f in features if not f.is_shared_overhead}
    res = MonthAttribution()
    by_acct: dict[str, list[Usage]] = defaultdict(list)
    for u in usage:
        if u.period_month == month and u.feature_id in product:
            by_acct[u.account_id].append(u)

    for a in accounts:
        mrr = a.arr_usd / 12.0
        res.total_mrr += mrr
        if a.arr_usd > 0:
            res.paying_accounts += 1
            rank_a = TIER_RANK.get(a.plan_tier, 0)
            for f in product.values():
                if not f.tier_gate or TIER_RANK.get(f.tier_gate, 99) <= rank_a:
                    res.eligible_accounts[f.id] += 1
        rows = by_acct.get(a.id, [])
        e = {u.feature_id: engagement(u.active_days, u.active_users) for u in rows}
        e_total = sum(e.values())
        if e_total <= 0:
            res.unattributed_mrr += mrr
            continue

        rank = TIER_RANK.get(a.plan_tier, 0)
        gated = {fid: v for fid, v in e.items()
                 if product[fid].tier_gate and TIER_RANK.get(product[fid].tier_gate, 99) <= rank}
        gated_total = sum(gated.values())
        uplift = settings.tier_uplift_share.get(a.plan_tier, 0.0) if gated_total > 0 else 0.0

        acct_days = max(account_active_days.get((month, a.id), 0), max(u.active_days for u in rows))
        for u in rows:
            fid = u.feature_id
            share = e[fid] / e_total
            v_u = mrr * share
            v_t = (1 - uplift) * mrr * share + (uplift * mrr * gated[fid] / gated_total if fid in gated else 0.0)
            res.v_usage[fid] += v_u
            res.v_tier[fid] += v_t
            if u.active_days > 0 and a.arr_usd > 0:
                res.active_accounts[fid] += 1
            conc = u.active_days / acct_days if acct_days else 0.0
            risk = churn_risk(conc, a.renewal_date, a.health, month, settings.critical_concentration) \
                if a.arr_usd > 0 else 0.0
            res.revenue_at_risk[fid] += a.arr_usd * risk
            if conc >= settings.critical_concentration and a.arr_usd >= settings.anchor_min_arr_usd:
                res.anchor_accounts[fid] += 1
            if u.active_days > 0:
                res.per_account.append(AccountFeatureValue(
                    a.id, fid, u.active_days, acct_days, round(conc, 4), risk, round(v_u, 2),
                    a.arr_usd, a.renewal_date))
    return res
