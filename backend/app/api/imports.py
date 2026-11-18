"""CSV import + manifest apply endpoints (real-data mode without live connectors).

    curl -X POST "localhost:8000/api/v1/import/accounts?period=2026-10" --data-binary @accounts.csv
The body is the raw CSV. All-or-nothing: on any invalid row nothing is written and the response
lists every error with its line number.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import config
from ..core.manifest import ManifestError, parse_manifest
from ..db import org_tx
from ..graph.manifest_store import active_manifest_text, record_and_apply
from ..graph.reallocate import load_resolver, reallocate
from ..importers.csv_import import ImportError_, parse_csv
from .deps import org_for, parse_period

router = APIRouter(prefix="/api/v1", tags=["import"])


async def _manifest(org: str):
    async with org_tx(org) as conn:
        text = await active_manifest_text(conn)
    try:
        return parse_manifest(text)
    except ManifestError as e:
        raise HTTPException(422, {"message": "the active features.yaml is invalid", "errors": e.errors}) from e


@router.post("/manifest/apply")
async def manifest_apply(org: str = Depends(org_for("manifest:sync"))):
    """Re-read features.yaml from disk: validate, record, apply (teams, features, Tier 1 mappings), re-allocate."""
    async with org_tx(org) as conn:
        res = await record_and_apply(conn, org, config.manifest_path.read_text(), source="disk", path="features.yaml")
        if res.status == "invalid":
            raise HTTPException(422, {"message": "features.yaml is invalid; the active manifest is unchanged",
                                      "errors": res.errors})
        moved = await reallocate(conn, org, await load_resolver(conn, parse_manifest(config.manifest_path.read_text())))
    return {"status": res.status, **res.summary, "reallocated_prs": moved["reallocated"]}


@router.post("/import/{kind}")
async def import_csv(kind: Literal["accounts", "usage", "tickets", "costs"], request: Request,
                     period: str | None = None, org: str = Depends(org_for("imports:write"))):
    m = await _manifest(org)
    text = (await request.body()).decode("utf-8-sig", errors="replace")
    try:
        parsed = parse_csv(kind, text, m)
    except ImportError_ as e:
        raise HTTPException(422, {"message": f"{len(e.errors)} problem(s) in the file; nothing was imported",
                                  "errors": e.errors[:200]}) from e
    month = parse_period(period)
    if kind in ("accounts", "usage", "costs") and month is None:
        raise HTTPException(422, "add ?period=YYYY-MM: the month these numbers describe")

    async with org_tx(org) as conn:
        feats = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
        unknown = sorted({r.get("feature_key") for r in parsed.rows if r.get("feature_key")} - set(feats))
        if unknown:
            raise HTTPException(409, f"features in features.yaml but not in the database: {unknown}. "
                                     "Run POST /api/v1/manifest/apply first.")
        if kind == "accounts":
            for r in parsed.rows:
                aid = await conn.fetchval("""
                    INSERT INTO accounts (org_id, external_ref, name, plan_tier, arr_usd, renewal_date, health, csm_email)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::account_health, $8)
                    ON CONFLICT (org_id, external_ref) DO UPDATE SET name = EXCLUDED.name,
                        plan_tier = EXCLUDED.plan_tier, arr_usd = EXCLUDED.arr_usd,
                        renewal_date = EXCLUDED.renewal_date, health = EXCLUDED.health, csm_email = EXCLUDED.csm_email
                    RETURNING id""", org, r["external_ref"], r["name"], r["plan_tier"], r["arr_usd"],
                    r["renewal_date"], r["health"], r["csm_email"])
                await conn.execute("""
                    INSERT INTO account_snapshots (org_id, account_id, period_month, arr_usd, plan_tier, health)
                    VALUES ($1, $2, $3, $4, $5, $6::account_health)
                    ON CONFLICT (org_id, period_month, account_id) DO UPDATE SET arr_usd = EXCLUDED.arr_usd,
                        plan_tier = EXCLUDED.plan_tier, health = EXCLUDED.health""",
                    org, aid, month, r["arr_usd"], r["plan_tier"], r["health"])
            return {"imported": len(parsed.rows), "kind": kind, "period": str(month)[:7]}

        accounts = {r["external_ref"]: r["id"] for r in await conn.fetch("SELECT id, external_ref FROM accounts")}
        if kind == "usage":
            missing = sorted({r["account_ref"] for r in parsed.rows} - set(accounts))
            if missing:
                raise HTTPException(409, f"{len(missing)} account_ref(s) not found; import accounts.csv first: "
                                         f"{missing[:10]}")
            await conn.execute("DELETE FROM usage_signals WHERE period_month = $1", month)
            await conn.execute("DELETE FROM account_activity_monthly WHERE period_month = $1", month)
            per_acct_days: dict[str, int] = defaultdict(int)
            for r in parsed.rows:
                await conn.execute("""
                    INSERT INTO usage_signals (org_id, period_month, account_id, feature_id, events, active_days,
                                               active_users) VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    org, month, accounts[r["account_ref"]], feats[r["feature_key"]], r["events"], r["active_days"],
                    r["active_users"])
                d = r["account_active_days"] if r["account_active_days"] is not None else r["active_days"]
                per_acct_days[r["account_ref"]] = max(per_acct_days[r["account_ref"]], d)
            for ref, d in per_acct_days.items():
                await conn.execute("""INSERT INTO account_activity_monthly (org_id, period_month, account_id, active_days)
                                      VALUES ($1, $2, $3, $4)""", org, month, accounts[ref], min(d, 31))
            return {"imported": len(parsed.rows), "kind": kind, "period": str(month)[:7],
                    "accounts": len(per_acct_days)}

        if kind == "tickets":
            n = 0
            for r in parsed.rows:
                await conn.execute("""
                    INSERT INTO support_tickets (org_id, external_id, account_id, feature_id, mapping_source,
                                                 mapping_confidence, created_at, handle_minutes)
                    VALUES ($1, $2, $3, $4, $5::mapping_source, $6, $7, $8)
                    ON CONFLICT (org_id, external_id) DO UPDATE SET account_id = EXCLUDED.account_id,
                        feature_id = coalesce(support_tickets.feature_id, EXCLUDED.feature_id),
                        handle_minutes = EXCLUDED.handle_minutes""",
                    org, r["external_id"], accounts.get(r["account_ref"] or ""),
                    feats.get(r["feature_key"]) if r["feature_key"] else None,
                    "manifest" if r["feature_key"] else None, 1 if r["feature_key"] else None,
                    r["created_at"], r["handle_minutes"])
                n += 1
            return {"imported": n, "kind": kind, "unmapped": sum(1 for r in parsed.rows if not r["feature_key"])}

        # costs: replace this month's imported lines
        await conn.execute("DELETE FROM cost_events WHERE source = 'csv_import' AND period_month = $1", month)
        for r in parsed.rows:
            await conn.execute("""
                INSERT INTO cost_events (org_id, period_month, feature_id, cost_type, source, amount_usd, unit)
                VALUES ($1, $2, $3, $4::cost_type, 'csv_import', $5, 'usd')""",
                org, month, feats.get(r["feature_key"]) if r["feature_key"] else None, r["cost_type"], r["amount_usd"])
        return {"imported": len(parsed.rows), "kind": kind, "period": str(month)[:7]}
