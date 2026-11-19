"""How much of each number is measured, and how much is modelled.

A feature P&L is an allocation model, not accounting truth. This module says, per feature and month,
which parts of the cost rest on declared or measured data and which on inference or estimates, and
whether the verdict survives a change of revenue attribution model. The dashboard shows the result
as a High / Medium / Low badge with the reasons, and as a "why this number" breakdown.

    engineering  share of the feature's effort that comes from manifest/human mappings (declared)
                 vs auto-inferred (Tier 2/3) mappings, weighted by each PR's effort
    support      share of the feature's tickets with a measured handle time (vs estimated), and how they
                 were mapped (declared vs inferred)
    infra / AI   share of cost directly tagged to the feature vs spread proportionally from untagged spend
    value        is there any usage + revenue data at all; do the usage-weighted and tier-gated models
                 agree on the sign of the monthly net (the verdict), and by how much they differ

    cost_quality = sum over cost parts of (part cost x part quality) / total cost, where quality is
                   declared 1.0, inferred 0.6, estimated 0.5, unmeasured 0 (see _QUALITY); engineering is
                   further capped at 0.85 because its hours are modelled, never measured
    level        high   cost_quality >= 0.80, value data present, verdict stable across models
                 low    cost_quality <  0.55, or no value data, or the verdict flips between models
                 medium otherwise
"""
from __future__ import annotations

from datetime import date
from typing import Any

_QUALITY = {"declared": 1.0, "inferred": 0.6, "estimated": 0.5}
# Engineering hours are always modelled (PR size, files, review rounds, calibrated to team capacity), never
# timesheets: even a perfectly declared mapping cannot make that line better than this.
ENG_MODEL_CEILING = 0.85
HIGH, LOW = 0.80, 0.55


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def assess(row: dict[str, Any]) -> dict[str, Any]:
    """Pure: one feature-month of evidence -> confidence. `row` keys are produced by load_evidence()."""
    eng_total = _f(row.get("eng_weight"))
    eng_declared = _f(row.get("eng_declared")) / eng_total if eng_total else 0.0
    eng_q = ENG_MODEL_CEILING * (eng_declared * _QUALITY["declared"] + (1 - eng_declared) * _QUALITY["inferred"]) \
        if eng_total else 0.0

    tickets = int(row.get("tickets") or 0)
    measured = (int(row.get("tickets_measured") or 0) / tickets) if tickets else 1.0
    t_declared = (int(row.get("tickets_declared") or 0) / tickets) if tickets else 1.0
    sup_q = (measured * 1.0 + (1 - measured) * _QUALITY["estimated"]) * \
        (t_declared * 1.0 + (1 - t_declared) * _QUALITY["inferred"])

    infra_total = _f(row.get("infra_total"))
    tagged = _f(row.get("infra_tagged")) / infra_total if infra_total else 1.0
    infra_q = tagged * 1.0 + (1 - tagged) * _QUALITY["estimated"]

    c_eng, c_sup = _f(row.get("c_eng")), _f(row.get("c_support"))
    c_inf = _f(row.get("c_infra")) + _f(row.get("c_ai_tokens"))
    c_tot = c_eng + c_sup + c_inf
    cost_quality = (c_eng * eng_q + c_sup * sup_q + c_inf * infra_q) / c_tot if c_tot else 0.0

    v_usage, v_tier = _f(row.get("v_usage")), _f(row.get("v_tier"))
    overhead = bool(row.get("is_shared_overhead"))
    has_value = overhead or (bool(row.get("accounts_with_revenue")) and (v_usage > 0 or v_tier > 0))
    net_usage, net_tier = v_usage - c_tot, v_tier - c_tot
    stable = overhead or (has_value and (net_usage >= 0) == (net_tier >= 0))
    spread = abs(v_usage - v_tier) / max(v_usage, v_tier, 1.0)

    reasons: list[str] = []
    if eng_total and eng_declared < 0.7:
        reasons.append(f"{1 - eng_declared:.0%} of engineering effort comes from inferred code mappings, "
                       "not features.yaml or a person")
    if row.get("team_unmapped_share", 0) > 0.15:
        reasons.append(f"{row['team_unmapped_share']:.0%} of the owning team's hours map to no feature "
                       "(this feature's share may be understated)")
    if tickets and measured < 0.7:
        reasons.append(f"support time is estimated for {1 - measured:.0%} of its {tickets} ticket(s)")
    if tickets and t_declared < 0.7:
        reasons.append(f"{1 - t_declared:.0%} of its tickets were matched to it by inference")
    if infra_total and tagged < 0.7:
        reasons.append(f"{1 - tagged:.0%} of its infra/AI cost is spread from untagged spend, not tagged to it")
    if overhead:
        reasons.append("shared platform cost: it has no revenue side by design, only the cost is assessed")
    elif not has_value:
        reasons.append("no usage or revenue data: the value side is unknown (cost alone never makes a zombie)")
    elif not stable and not overhead:
        reasons.append(f"the verdict flips between attribution models (usage-weighted net ${net_usage:,.0f}, "
                       f"tier-gated net ${net_tier:,.0f})")
    elif spread > 0.5 and not overhead:
        reasons.append(f"attributed revenue differs by {spread:.0%} between models (the verdict holds)")

    if cost_quality >= HIGH and has_value and stable:
        level = "high"
    elif cost_quality < LOW or not has_value or not stable:
        level = "low"
    else:
        level = "medium"
    if not reasons:
        reasons = ["costs rest on declared mappings and measured data; the verdict holds under every model"]
    return {
        "level": level, "score": round(min(cost_quality, 1.0) * (1.0 if stable else 0.5 if has_value else 0.3), 2),
        "cost_quality": round(cost_quality, 3), "verdict_stable": stable, "has_value_data": has_value,
        "reasons": reasons,
        "coverage": {"eng_declared_share": round(eng_declared, 3) if eng_total else None,
                     "support_measured_share": round(measured, 3) if tickets else None,
                     "support_declared_share": round(t_declared, 3) if tickets else None,
                     "infra_tagged_share": round(tagged, 3) if infra_total else None,
                     "team_unmapped_share": round(row.get("team_unmapped_share", 0.0), 3),
                     "value_model_spread": round(spread, 3)},
        "net_by_model": {"usage_weighted": round(net_usage, 2), "tier_gated": round(net_tier, 2)},
    }


async def load_evidence(conn, period: date | None = None) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(_SQL, period)
    out = {}
    for r in rows:
        d = dict(r)
        d["team_unmapped_share"] = _f(d["team_unmapped_share"])
        out[d.pop("key")] = d
    return out


_SQL = """
WITH m AS (SELECT coalesce($1::date, (SELECT max(period_month) FROM feature_pnl_monthly)) AS month),
eng AS (
    SELECT a.feature_id,
           sum(a.share * coalesce(w.raw_effort_hours, 1 + (w.additions + w.deletions) / 50.0)) AS eng_weight,
           sum(a.share * coalesce(w.raw_effort_hours, 1 + (w.additions + w.deletions) / 50.0))
               FILTER (WHERE a.source IN ('manifest', 'human')) AS eng_declared
      FROM work_item_allocations a JOIN work_items w ON w.id = a.work_item_id CROSS JOIN m
     WHERE w.period_month = m.month GROUP BY a.feature_id),
sup AS (
    SELECT t.feature_id, count(*) AS tickets,
           count(*) FILTER (WHERE NOT t.handle_time_estimated) AS tickets_measured,
           count(*) FILTER (WHERE t.mapping_source IN ('manifest', 'human')) AS tickets_declared
      FROM support_tickets t CROSS JOIN m
     WHERE t.period_month = m.month AND t.feature_id IS NOT NULL GROUP BY t.feature_id),
inf AS (
    SELECT c.feature_id, sum(c.amount_usd) AS infra_total,
           sum(c.amount_usd) FILTER (WHERE c.source <> 'featureledger.model') AS infra_tagged
      FROM cost_events c CROSS JOIN m
     WHERE c.period_month = m.month AND c.cost_type IN ('infra', 'llm') AND c.feature_id IS NOT NULL
     GROUP BY c.feature_id),
cap AS (
    SELECT e.team_id, e.unmapped_hours / nullif(e.capacity_hours, 0) AS team_unmapped_share
      FROM eng_capacity_monthly e CROSS JOIN m WHERE e.period_month = m.month)
SELECT f.key, f.is_shared_overhead, p.c_eng, p.c_support, p.c_infra, p.c_ai_tokens, p.v_usage, p.v_tier, p.active_accounts,
       (p.active_accounts > 0 AND p.eligible_accounts > 0) AS accounts_with_revenue,
       eng.eng_weight, eng.eng_declared, sup.tickets, sup.tickets_measured, sup.tickets_declared,
       inf.infra_total, inf.infra_tagged, coalesce(cap.team_unmapped_share, 0) AS team_unmapped_share
  FROM feature_pnl_monthly p
  JOIN features f ON f.id = p.feature_id
  CROSS JOIN m
  LEFT JOIN eng ON eng.feature_id = p.feature_id
  LEFT JOIN sup ON sup.feature_id = p.feature_id
  LEFT JOIN inf ON inf.feature_id = p.feature_id
  LEFT JOIN cap ON cap.team_id = f.owner_team_id
 WHERE p.period_month = m.month"""


async def confidence_by_feature(conn, period: date | None = None) -> dict[str, dict[str, Any]]:
    return {k: assess(v) for k, v in (await load_evidence(conn, period)).items()}
