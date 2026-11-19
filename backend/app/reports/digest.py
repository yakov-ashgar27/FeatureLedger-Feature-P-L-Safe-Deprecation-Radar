"""Executive Monthly Digest.

    build_digest(conn, period)  -> summary dict (pure reads, RLS-scoped)
    slack_blocks(summary)       -> Block Kit payload
    render_html(summary)        -> self-contained, print-ready Executive Brief (browser "Save as PDF")
    post_to_slack(blocks)       -> posted | dry_run | skipped | failed

Definitions (docs/SPEC.md §14):
    zombie tax outstanding   trailing-3-month average monthly cost / engineering hours of features classified zombie
    reclaimed this month     sunset plans archived in the period; their trailing-3-month averages frozen at archive
    top bleeders             features ranked by monthly bleed = max(net loss, AI overspend), where
                             AI overspend = c_ai_tokens + c_infra - (1 - target GM) * v_usage  (target = margin_bleed_max_gross_margin)
"""
from __future__ import annotations

import html
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from ..config import config

log = logging.getLogger("featureledger.digest")


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def previous_month(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def _f(v) -> float:
    return float(v) if v is not None else 0.0


async def build_digest(conn, period: date) -> dict[str, Any]:
    period = month_start(period)
    nxt = date(period.year + (period.month == 12), period.month % 12 + 1, 1)
    org = await conn.fetchrow("SELECT name, slug, is_demo FROM organizations LIMIT 1")
    s = await conn.fetchrow("SELECT * FROM org_settings")
    per_fte = _f(s["hours_per_engineer_month"]) * _f(s["allocatable_fraction"]) if s else 112.0
    target_gm = _f(s["margin_bleed_max_gross_margin"]) if s else 0.2
    latest = await conn.fetchval("SELECT max(period_month) FROM feature_pnl_monthly")

    feats = await conn.fetch("""
        WITH win AS (
            SELECT p.feature_id, avg(p.c_total) c, avg(p.eng_hours) h, avg(p.v_usage) v, avg(p.c_ai_tokens) ai,
                   avg(p.c_infra) infra
              FROM feature_pnl_monthly p
             WHERE p.period_month > $1::date - interval '3 months' GROUP BY p.feature_id)
        SELECT f.key, f.name, f.is_shared_overhead, cur.classification::text AS classification, cur.margin_bleed,
               cur.margin_bleed_reason, cur.gross_margin_pct, cur.active_accounts, cur.eligible_accounts,
               win.c, win.h, win.v, win.ai, win.infra
          FROM features f JOIN feature_pnl_monthly cur ON cur.feature_id = f.id AND cur.period_month = $1
          JOIN win ON win.feature_id = f.id
         WHERE NOT f.is_shared_overhead""", latest) if latest else []

    zombies = [r for r in feats if r["classification"] == "zombie"]
    outstanding = {
        "cost_month": round(sum(_f(r["c"]) for r in zombies), 2),
        "eng_hours_month": round(sum(_f(r["h"]) for r in zombies), 1),
        "fte": round(sum(_f(r["h"]) for r in zombies) / per_fte, 2),
        "features": [r["name"] for r in zombies],
    }

    bleeders = []
    for r in feats:
        c, v, ai, infra = _f(r["c"]), _f(r["v"]), _f(r["ai"]), _f(r["infra"])
        net_loss = max(0.0, c - v)
        ai_over = max(0.0, ai + infra - (1 - target_gm) * v) if ai > 0 else 0.0
        bleed = max(net_loss, ai_over)
        if bleed <= 0 and not r["margin_bleed"]:
            continue
        kind = "margin_bleed" if r["margin_bleed"] or ai_over > net_loss else "net_loss"
        bleeders.append({
            "key": r["key"], "name": r["name"], "kind": kind, "classification": r["classification"],
            "bleed_month": round(bleed, 2), "net_month": round(v - c, 2), "ai_tokens_month": round(ai, 2),
            "gross_margin_pct": round(float(r["gross_margin_pct"]), 4) if r["gross_margin_pct"] is not None else None,
            "adoption": round(r["active_accounts"] / r["eligible_accounts"], 4) if r["eligible_accounts"] else 0.0,
            "reason": r["margin_bleed_reason"] if kind == "margin_bleed" else
            f"Costs ${c:,.0f}/mo against ${v:,.0f}/mo attributed revenue"})
    bleeders.sort(key=lambda b: -b["bleed_month"])
    top = bleeders[:3]
    bleed_keys = {b["key"] for b in top}
    for b in bleeders:   # an AI margin-bleed feature always makes the list
        if b["kind"] == "margin_bleed" and b["key"] not in bleed_keys and len(top) >= 3:
            top[-1] = b
            break

    plans = await conn.fetch("""
        SELECT sp.state::text AS state, sp.reclaimed, sp.state_entered_at, sp.rollback_count, f.name, f.key
          FROM sunset_plans sp JOIN features f ON f.id = sp.feature_id""")
    done_period = [p for p in plans if p["state"] == "archived" and p["reclaimed"]
                   and period <= datetime.fromisoformat(p["reclaimed"]["archived_at"]).date() < nxt]
    done_all = [p for p in plans if p["state"] == "archived" and p["reclaimed"]]

    def total(ps):
        return {"cost_month": round(sum(p["reclaimed"]["cost_month"] for p in ps), 2),
                "eng_hours_month": round(sum(p["reclaimed"]["eng_hours_month"] for p in ps), 1),
                "fte": round(sum(p["reclaimed"]["eng_hours_month"] for p in ps) / per_fte, 2),
                "annualized": round(12 * sum(p["reclaimed"]["cost_month"] for p in ps), 2), "sunsets": len(ps)}

    in_flight = [{"feature": p["name"], "key": p["key"], "state": p["state"], "rollbacks": p["rollback_count"],
                  "days_in_state": (datetime.now(timezone.utc) - p["state_entered_at"]).days}
                 for p in plans if p["state"] not in ("archived", "cancelled")]

    health = await data_health(conn, latest)
    ai_total = round(sum(_f(r["ai"]) for r in feats), 2)
    return {
        "org": {"name": org["name"] if org else "", "slug": org["slug"] if org else "", "demo": bool(org and org["is_demo"])},
        "period": period.isoformat()[:7], "ledger_period": latest.isoformat()[:7] if latest else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "zombie_tax": {"outstanding": outstanding, "reclaimed_this_month": total(done_period),
                       "reclaimed_to_date": total(done_all)},
        "sunsets_completed": [{"feature": p["name"], "key": p["key"], **p["reclaimed"]} for p in done_period],
        "sunsets_in_flight": in_flight,
        "top_bleeders": top,
        "ai": {"token_spend_month": ai_total,
               "margin_bleed_features": [r["name"] for r in feats if r["margin_bleed"]]},
        "data_health": health,
        "links": {"brief": f"{config.app_url}/reports?period={period.isoformat()[:7]}", "dashboard": config.app_url},
    }


async def data_health(conn, latest: date | None) -> dict[str, Any]:
    checks = []
    integ = await conn.fetch("SELECT kind::text AS kind, status::text AS status, sandbox, last_error FROM integrations")
    failing = [i["kind"] for i in integ if i["status"] == "error" or i["last_error"]]
    checks.append({"name": "Connectors", "ok": not failing,
                   "detail": f"{len(integ) - len(failing)}/{len(integ)} healthy" + (f"; failing: {', '.join(failing)}" if failing else "")})
    dead = await conn.fetchval("SELECT count(*) FROM ingest_inbox WHERE dead_lettered_at IS NOT NULL")
    checks.append({"name": "Dead letters", "ok": dead == 0, "detail": f"{dead} item(s) waiting for a retry"})
    cap = await conn.fetchrow("""SELECT coalesce(sum(capacity_hours), 0) cap, coalesce(sum(unmapped_hours), 0) unmapped
                                   FROM eng_capacity_monthly WHERE period_month = $1""", latest) if latest else None
    share = (_f(cap["unmapped"]) / _f(cap["cap"])) if cap and _f(cap["cap"]) else 0.0
    checks.append({"name": "Unmapped engineering", "ok": share <= 0.10, "detail": f"{share:.1%} of capacity has no feature"})
    pending = await conn.fetchval("SELECT count(*) FROM mapping_suggestions WHERE status = 'pending' AND decided_at IS NULL")
    checks.append({"name": "Triage queue", "ok": pending <= 25, "detail": f"{pending} mapping(s) waiting for a decision"})
    untagged = await conn.fetchval("""SELECT coalesce(sum(amount_usd), 0) FROM cost_events
                                       WHERE feature_id IS NULL AND period_month = $1""", latest) if latest else 0
    tagged = await conn.fetchval("""SELECT coalesce(sum(amount_usd), 0) FROM cost_events WHERE feature_id IS NOT NULL
                                      AND cost_type IN ('infra', 'llm') AND period_month = $1""", latest) if latest else 0
    ushare = _f(untagged) / max(_f(untagged) + _f(tagged), 1.0)
    checks.append({"name": "Untagged cloud spend", "ok": ushare <= 0.25, "detail": f"{ushare:.0%} of infra cost has no feature tag"})
    months = await conn.fetchval("SELECT count(DISTINCT period_month) FROM feature_pnl_monthly")
    checks.append({"name": "History", "ok": months >= 3, "detail": f"{months} month(s) of P&L"})
    passed = sum(1 for c in checks if c["ok"])
    status = "healthy" if passed == len(checks) else ("attention" if passed >= len(checks) - 2 else "poor")
    return {"status": status, "score": round(passed / len(checks), 2), "checks": checks}


# ------------------------------------------------------------------------------------- Slack
def _usd(v: float) -> str:
    return f"${v / 1000:,.1f}k" if abs(v) >= 10_000 else f"${v:,.0f}"


def _pct(v: float | None) -> str:
    return "" if v is None else f"{v:.0%}"


def slack_blocks(d: dict[str, Any]) -> dict[str, Any]:
    zt = d["zombie_tax"]
    rec, out = zt["reclaimed_this_month"], zt["outstanding"]
    hdr = f"FeatureLedger · Executive digest · {d['org']['name']} · {d['period']}"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": hdr[:150]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Zombie tax reclaimed*\n{_usd(rec['cost_month'])}/mo · {rec['eng_hours_month']:,.0f} h · {rec['fte']} FTE"},
            {"type": "mrkdwn", "text": f"*Sunsets completed*\n{rec['sunsets']} this month · {zt['reclaimed_to_date']['sunsets']} to date"},
            {"type": "mrkdwn", "text": f"*Zombie tax still running*\n{_usd(out['cost_month'])}/mo · {out['fte']} FTE"},
            {"type": "mrkdwn", "text": f"*AI token spend*\n{_usd(d['ai']['token_spend_month'])}/mo"},
        ]},
    ]
    if d["sunsets_completed"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Completed sunsets*\n" + "\n".join(
            f"• {s['feature']}: {_usd(s['cost_month'])}/mo, {s['eng_hours_month']:,.0f} h/mo reclaimed"
            for s in d["sunsets_completed"])}})
    if d["top_bleeders"]:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Top bleeding features*\n" + "\n".join(
            f"{i}. *{b['name']}* {'🔥 AI margin bleed' if b['kind'] == 'margin_bleed' else '🧟 net loss'}: "
            f"{_usd(b['bleed_month'])}/mo"
            + (f" · GM {b['gross_margin_pct']:.0%}" if b["gross_margin_pct"] is not None and b["kind"] == "margin_bleed" else "")
            for i, b in enumerate(d["top_bleeders"], 1))}})
    h = d["data_health"]
    icon = {"healthy": "🟢", "attention": "🟡", "poor": "🔴"}[h["status"]]
    bad = [c for c in h["checks"] if not c["ok"]]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
        f"{icon} Data health: {h['status']} ({int(h['score'] * 100)}%)" + (
            " · " + "; ".join(f"{c['name']}: {c['detail']}" for c in bad) if bad else "")}]})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "Open the Executive Brief"},
         "url": d["links"]["brief"], "style": "primary"},
        {"type": "button", "text": {"type": "plain_text", "text": "Dashboard"}, "url": d["links"]["dashboard"]}]})
    return {"text": f"{hdr}: {_usd(rec['cost_month'])}/mo reclaimed, {len(d['top_bleeders'])} bleeding feature(s)",
            "blocks": blocks}


async def post_to_slack(payload: dict[str, Any], *, demo: bool) -> tuple[str, str | None]:
    url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if demo or config.sandbox_mode:
        return "dry_run", None
    if not url:
        return "skipped", "SLACK_WEBHOOK_URL is not set"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, content=json.dumps(payload), headers={"Content-Type": "application/json"})
        if r.status_code >= 300:
            return "failed", f"Slack answered HTTP {r.status_code}: {r.text[:200]}"
        return "posted", None
    except Exception as e:  # noqa: BLE001
        return "failed", str(e)[:300]


# ------------------------------------------------------------------------------------- brief
def render_html(d: dict[str, Any]) -> str:
    e = html.escape
    zt = d["zombie_tax"]
    rec, out, todate = zt["reclaimed_this_month"], zt["outstanding"], zt["reclaimed_to_date"]

    def kpi(label: str, value: str, sub: str) -> str:
        return f'<div class="kpi"><div class="l">{e(label)}</div><div class="v">{e(value)}</div><div class="s">{e(sub)}</div></div>'

    rows = "".join(
        f"<tr><td>{i}</td><td><b>{e(b['name'])}</b><div class='s'>{e(b['reason'] or '')}</div></td>"
        f"<td>{'AI margin bleed' if b['kind'] == 'margin_bleed' else 'Net loss'}</td>"
        f"<td class='n'>{e(_usd(b['bleed_month']))}</td>"
        f"<td class='n'>{_pct(b['gross_margin_pct'])}</td>"
        f"<td class='n'>{b['adoption']:.1%}</td></tr>" for i, b in enumerate(d["top_bleeders"], 1)) or \
        "<tr><td colspan=6>No feature is bleeding this month.</td></tr>"
    done = "".join(f"<li><b>{e(s['feature'])}</b>: {e(_usd(s['cost_month']))}/mo and {s['eng_hours_month']:,.0f} engineering h/mo reclaimed</li>"
                   for s in d["sunsets_completed"]) or "<li>No sunset completed this month.</li>"
    flight = "".join(f"<li>{e(s['feature'])}: {e(s['state'].replace('_', ' '))} ({s['days_in_state']} d)</li>"
                     for s in d["sunsets_in_flight"]) or "<li>None.</li>"
    checks = "".join(f"<li class='{'ok' if c['ok'] else 'bad'}'>{'✓' if c['ok'] else '!'} {e(c['name'])}: {e(c['detail'])}</li>"
                     for c in d["data_health"]["checks"])
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Executive Brief · {e(d['org']['name'])} · {e(d['period'])}</title>
<style>
@page {{ size: A4; margin: 16mm 14mm; }}
* {{ box-sizing: border-box; }}
body {{ font: 13px/1.45 -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif; color: #1d1d1b; max-width: 900px; margin: 24px auto; padding: 0 20px; }}
h1 {{ font-size: 22px; margin: 0; letter-spacing: -.01em; }} h2 {{ font-size: 14px; margin: 26px 0 8px; text-transform: uppercase; letter-spacing: .06em; color: #57564f; }}
.meta {{ color: #6b6a63; margin-top: 4px; }} .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 18px; }}
.kpi {{ border: 1px solid #dddcd6; border-radius: 8px; padding: 10px 12px; break-inside: avoid; }} .kpi .l {{ font-size: 11px; color: #6b6a63; text-transform: uppercase; letter-spacing: .05em; }}
.kpi .v {{ font-size: 20px; font-weight: 650; margin-top: 2px; font-variant-numeric: tabular-nums; }} .s {{ font-size: 11px; color: #6b6a63; }}
table {{ width: 100%; border-collapse: collapse; }} td, th {{ text-align: left; padding: 7px 6px; border-bottom: 1px solid #ecebe6; vertical-align: top; }}
th {{ font-size: 11px; color: #6b6a63; font-weight: 600; }} .n {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
ul {{ margin: 0; padding-left: 18px; }} li {{ margin: 3px 0; }} li.bad {{ color: #8a3413; }} li.ok {{ color: #57564f; }} .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
.foot {{ margin-top: 28px; color: #8d8c85; font-size: 11px; border-top: 1px solid #ecebe6; padding-top: 8px; }}
@media print {{ body {{ margin: 0; }} a {{ color: inherit; text-decoration: none; }} }}
</style></head><body>
<h1>Executive Brief: feature economics</h1>
<div class="meta">{e(d['org']['name'])} · {e(d['period'])} · ledger through {e(d['ledger_period'] or 'n/a')}{' · live demo data' if d['org']['demo'] else ''}</div>
<div class="grid">
{kpi('Zombie tax reclaimed', _usd(rec['cost_month']) + '/mo', f"{rec['eng_hours_month']:,.0f} h · {rec['fte']} FTE · {rec['sunsets']} sunset(s)")}
{kpi('Reclaimed to date', _usd(todate['annualized']) + '/yr', f"{todate['fte']} FTE across {todate['sunsets']} sunset(s)")}
{kpi('Zombie tax still running', _usd(out['cost_month']) + '/mo', f"{out['eng_hours_month']:,.0f} h · {out['fte']} FTE")}
{kpi('AI token spend', _usd(d['ai']['token_spend_month']) + '/mo', f"{len(d['ai']['margin_bleed_features'])} feature(s) bleeding margin")}
</div>
<h2>Top bleeding features</h2>
<table><tr><th>#</th><th>Feature</th><th>Why</th><th class="n">Bleed / mo</th><th class="n">Gross margin</th><th class="n">Adoption</th></tr>{rows}</table>
<div class="cols"><div><h2>Sunsets completed</h2><ul>{done}</ul></div><div><h2>In flight</h2><ul>{flight}</ul></div></div>
<h2>Data health: {e(d['data_health']['status'])}</h2><ul>{checks}</ul>
<div class="foot">Generated {e(d['generated_at'][:16].replace('T', ' '))} UTC by FeatureLedger. Engineering cost is aggregated by feature and team; no individual developer metrics.
Bleed = max(net loss, AI overspend vs. the target gross margin). Reclaimed values are trailing-3-month averages frozen when each sunset was archived.</div>
</body></html>"""
