"""Feature P&L assembly and health classification.

Per feature f and month m (Contribution view = direct, avoidable cost):
    C_eng     = calibrated_hours(f, m) * eng_hourly_rate
    C_support = sum(handle_minutes)/60 * support_hourly_rate      (tickets mapped to f)
    C_infra   = tagged infra + LLM spend for f
    C_total   = C_eng + C_support + C_infra
    Net_ROI   = V_total - C_total
    Efficiency= V_total / max(C_total, 1)
Fully-loaded view adds C_overhead_alloc: the cost of shared-overhead features spread by their
allocation_rule (proportional_usage | proportional_cost | fixed_weights; 'none' = platform line only).

Classification uses a trailing window (default 3 months) and is evaluated in a fixed order;
the first rule that matches wins, and the reason string records the evidence.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .attribution import MonthAttribution, attribute_month
from .effort import TeamCalibration, allocate_effort
from .types import AttributionModel, Dataset, Feature, FeatureClass, OrgSettings

MODEL_VERSION = "pnl-2026.11-v1"


@dataclass
class FeaturePnl:
    feature_id: str
    feature_key: str
    period_month: date
    eng_hours: float = 0.0
    bug_hours: float = 0.0
    c_eng: float = 0.0
    c_support: float = 0.0
    c_infra: float = 0.0
    c_overhead_alloc: float = 0.0
    v_usage: float = 0.0
    v_tier: float = 0.0
    revenue_at_risk_arr: float = 0.0
    tickets: int = 0
    active_accounts: int = 0
    eligible_accounts: int = 0
    anchor_accounts: int = 0
    classification: FeatureClass = FeatureClass.WATCH
    classification_reason: str = ""

    @property
    def c_total(self) -> float:
        return self.c_eng + self.c_support + self.c_infra

    @property
    def bug_tax_ratio(self) -> float:
        return self.bug_hours / self.eng_hours if self.eng_hours > 0 else 0.0

    @property
    def adoption(self) -> float:
        return self.active_accounts / self.eligible_accounts if self.eligible_accounts else 0.0

    def value(self, model: AttributionModel) -> float:
        if model is AttributionModel.TIER_GATED:
            return self.v_tier
        if model is AttributionModel.CRITICAL_DEPENDENCY:
            return self.revenue_at_risk_arr / 12.0
        return self.v_usage

    def net_roi(self, model: AttributionModel = AttributionModel.USAGE_WEIGHTED) -> float:
        return self.value(model) - self.c_total

    def efficiency(self, model: AttributionModel = AttributionModel.USAGE_WEIGHTED) -> float:
        return self.value(model) / max(self.c_total, 1.0)


@dataclass
class PnlResult:
    months: list[date]
    rows: dict[tuple[date, str], FeaturePnl]
    calibration: dict[tuple[date, str], TeamCalibration]
    attribution: dict[date, MonthAttribution]
    untagged_infra: dict[date, float] = field(default_factory=dict)

    def month_rows(self, m: date) -> list[FeaturePnl]:
        return [r for (mm, _), r in self.rows.items() if mm == m]


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def compute_pnl(ds: Dataset, months: list[date]) -> PnlResult:
    s = ds.settings
    months = sorted(months)
    feats = {f.id: f for f in ds.features}
    effort, cal = allocate_effort(ds.work_items, ds.teams, ds.features, s, months)

    rows: dict[tuple[date, str], FeaturePnl] = {}
    for m in months:
        for f in ds.features:
            rows[(m, f.id)] = FeaturePnl(f.id, f.key, m)

    for (m, fid), fe in effort.items():
        r = rows[(m, fid)]
        r.eng_hours = fe.hours
        r.bug_hours = fe.bug_hours
        r.c_eng = fe.hours * s.eng_hourly_rate_usd

    for t in ds.tickets:
        if t.feature_id in feats and (t.period_month, t.feature_id) in rows:
            r = rows[(t.period_month, t.feature_id)]
            r.tickets += 1
            r.c_support += t.handle_minutes / 60.0 * s.support_hourly_rate_usd

    untagged: dict[date, float] = defaultdict(float)
    for c in ds.cost_lines:
        if c.period_month not in months:
            continue
        if c.feature_id in feats:
            rows[(c.period_month, c.feature_id)].c_infra += c.amount_usd
        else:
            untagged[c.period_month] += c.amount_usd

    attributions: dict[date, MonthAttribution] = {}
    for m in months:
        att = attribute_month(m, ds.accounts_by_month.get(m, []), ds.usage, ds.account_active_days,
                              ds.features, s)
        attributions[m] = att
        for f in ds.features:
            r = rows[(m, f.id)]
            r.v_usage = att.v_usage.get(f.id, 0.0)
            r.v_tier = att.v_tier.get(f.id, 0.0)
            r.revenue_at_risk_arr = att.revenue_at_risk.get(f.id, 0.0)
            r.active_accounts = att.active_accounts.get(f.id, 0)
            r.anchor_accounts = att.anchor_accounts.get(f.id, 0)
            r.eligible_accounts = att.eligible_accounts.get(f.id, 0)
        _allocate_overhead(m, ds.features, rows)

    for i, m in enumerate(months):
        window = months[max(0, i - 2): i + 1]
        for f in ds.features:
            cls, why = classify(f, [rows[(w, f.id)] for w in window], s)
            rows[(m, f.id)].classification = cls
            rows[(m, f.id)].classification_reason = why

    return PnlResult(months, rows, cal, attributions, dict(untagged))


def _allocate_overhead(m: date, features: list[Feature], rows: dict[tuple[date, str], FeaturePnl]) -> None:
    product = [f for f in features if not f.is_shared_overhead]
    by_key = {f.key: f for f in product}
    for o in (f for f in features if f.is_shared_overhead):
        pool = rows[(m, o.id)].c_total
        if pool <= 0 or o.allocation_rule == "none":
            continue
        if o.allocation_rule == "proportional_usage":
            basis = {f.id: rows[(m, f.id)].v_usage for f in product}
        elif o.allocation_rule == "proportional_cost":
            basis = {f.id: rows[(m, f.id)].c_total for f in product}
        elif o.allocation_rule == "fixed_weights":
            basis = {by_key[k].id: w for k, w in (o.allocation_weights or {}).items() if k in by_key}
        else:
            raise ValueError(f"unknown allocation_rule {o.allocation_rule!r}")
        total = sum(basis.values())
        if total <= 0:
            continue
        for fid, b in basis.items():
            rows[(m, fid)].c_overhead_alloc += pool * b / total


def classify(f: Feature, window: list[FeaturePnl], s: OrgSettings) -> tuple[FeatureClass, str]:
    """window: oldest -> newest, the newest is the month being classified."""
    cur = window[-1]
    if f.is_shared_overhead:
        return FeatureClass.PLATFORM, "Shared infrastructure, tracked as platform overhead"
    if f.is_regulatory:
        return FeatureClass.MANDATED, "Regulatory/compliance requirement: exempt from sunset"

    n = len(window)
    c_avg = sum(r.c_total for r in window) / n
    net = sum(r.v_usage - r.c_total for r in window)
    eng = sum(r.eng_hours for r in window)
    bug_tax = sum(r.bug_hours for r in window) / eng if eng else 0.0
    adoption = cur.adoption
    trend = cur.active_accounts - window[0].active_accounts
    growth = [
        (b.active_accounts - a.active_accounts) / a.active_accounts
        for a, b in zip(window, window[1:]) if a.active_accounts > 0
    ]
    mom = sum(growth) / len(growth) if growth else 0.0
    age = _months_between(f.launched_at, cur.period_month) if f.launched_at else 999

    if adoption < s.anchor_max_adoption and cur.anchor_accounts > 0:
        return FeatureClass.NICHE_ANCHOR, (
            f"Adoption {adoption:.1%} but {cur.anchor_accounts} account(s) ≥${s.anchor_min_arr_usd:,.0f} ARR "
            f"depend on it; ${cur.revenue_at_risk_arr:,.0f} ARR at risk. Do not kill blindly")
    if (adoption < s.zombie_max_adoption and net < 0 and trend <= 0
            and (bug_tax >= s.zombie_min_bug_tax or c_avg >= s.zombie_min_monthly_cost)):
        return FeatureClass.ZOMBIE, (
            f"Adoption {adoption:.1%}, {n}-mo net ${net:,.0f}, bug tax {bug_tax:.0%}, "
            f"avg cost ${c_avg:,.0f}/mo, usage trend {trend:+d} accounts")
    if (age < s.growth_max_age_months or mom >= s.growth_min_mom_adoption) and cur.c_total >= cur.v_usage:
        return FeatureClass.GROWTH_BET, (
            f"Age {age if age < 999 else 'n/a'} mo, adoption growth {mom:+.0%}/mo, "
            f"investing ${cur.c_total:,.0f} vs ${cur.v_usage:,.0f} attributed")
    if cur.efficiency() >= s.cash_cow_min_efficiency:
        return FeatureClass.CASH_COW, f"Efficiency {cur.efficiency():.1f}x on ${cur.v_usage:,.0f}/mo attributed"
    return FeatureClass.WATCH, (
        f"Efficiency {cur.efficiency():.2f}x, adoption {adoption:.1%}: no rule triggered")
