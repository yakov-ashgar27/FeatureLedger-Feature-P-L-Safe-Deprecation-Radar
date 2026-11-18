"""Safe Sunset pipeline endpoints. Every state change writes a hash-chained audit row."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth.rbac import ACK_PERMISSION, ADVANCE_PERMISSION, CANARY_PROMOTE_PERMISSION, EVIDENCE_PERMISSION
from ..auth.session import Principal, check, require
from ..config import config
from ..core.sunset import SunsetState, TransitionError, advance, cancel, rollback
from ..core.sunset_artifacts import DossierAccount, blast_radius, cleanup_pr_outline, impact_dossier
from ..db import org_tx
from ..execution import orchestrator as ox
from ..graph.manifest_store import active_manifest
from .deps import current_org, row

router = APIRouter(prefix="/api/v1/sunset-plans", tags=["sunset"])
PLAN_SQL = ox.PLAN_SQL


def _http(e: Exception) -> HTTPException:
    if isinstance(e, TransitionError):
        return HTTPException(409, {"code": e.code, "message": str(e)})
    if isinstance(e, ox.ExecutionError):
        return HTTPException(e.status, {"code": e.code, "message": str(e)})
    if isinstance(e, ox.PlanNotFound):
        return HTTPException(404, "sunset plan not found")
    raise e


async def _load(conn, plan_id: str, for_update: bool = False):
    try:
        return await ox.load(conn, plan_id, for_update)
    except ox.PlanNotFound as e:
        raise _http(e) from e


def _check_version(p, expected: int | None) -> None:
    if expected is not None and expected != p["version"]:
        raise HTTPException(409, {"code": "stale_version",
                                  "message": f"plan changed (version {p['version']}); reload and retry"})


@router.get("")
async def list_plans(org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        latest = await conn.fetchval("SELECT max(period_month) FROM feature_pnl_monthly")
        rows = await conn.fetch(PLAN_SQL + " ORDER BY p.created_at")
        metrics = {str(r["feature_id"]): row(r) for r in await conn.fetch(
            """SELECT feature_id, eng_hours, c_total, revenue_at_risk_arr, active_accounts FROM feature_pnl_monthly
                WHERE period_month = $1""", latest)}
        flags = {r["flag_key"]: row(r) for r in await conn.fetch(
            "SELECT flag_key, state, provider, jsonb_array_length(keep_on_for) AS keep_on_for_count FROM flag_states")}
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        d = row(r)
        d["days_in_state"] = (now - r["state_entered_at"]).days
        d["metrics"] = metrics.get(str(r["feature_id"]))
        d["flag"] = flags.get(r["flag_key"]) or ({"state": "fully_on", "provider": None} if r["flag_key"] else None)
        pr = r["cleanup_pr"] or {}
        d["pull_request"] = {k: pr.get(k) for k in ("provider", "repo", "number", "url", "branch", "state")} \
            if pr.get("number") else None
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
async def create_plan(body: CreatePlan, p: Principal = Depends(require("sunset:create"))):
    org = p.org_id
    async with org_tx(org) as conn:
        f = await conn.fetchrow("SELECT * FROM features WHERE key = $1", body.feature_key)
        if not f:
            raise HTTPException(404, "feature not found")
        if f["is_regulatory"] or f["is_shared_overhead"]:
            raise HTTPException(422, "regulatory and shared-overhead features cannot be sunset")
        try:
            pid = await conn.fetchval("""
                INSERT INTO sunset_plans (org_id, feature_id, reason, scream_test_days, created_by)
                VALUES ($1, $2, $3, $4, $5::uuid) RETURNING id""", org, f["id"], body.reason, body.scream_test_days,
                p.user_id)
        except Exception as e:  # unique live plan per feature
            if "sunset_plans_one_live_uidx" in str(e):
                raise HTTPException(409, "this feature already has a live sunset plan") from e
            raise
        await conn.execute("UPDATE features SET status = 'candidate_for_sunset' WHERE id = $1", f["id"])
        await ox.audit(conn, org, pid, None, "candidate_for_sunset", "transition", {"reason": body.reason}, p)
        return row(await _load(conn, pid))


@router.get("/{plan_id}")
async def get_plan(plan_id: str, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id)
        log = await conn.fetch("""SELECT id, from_state::text, to_state::text, action, payload, created_at,
                                         encode(hash, 'hex') AS hash, encode(prev_hash, 'hex') AS prev_hash
                                    FROM sunset_audit_logs WHERE plan_id = $1 ORDER BY id""", p["id"])
        flag = await conn.fetchrow("""SELECT state, provider, jsonb_array_length(keep_on_for) AS keep_on_for_count,
                                             updated_at, last_error FROM flag_states WHERE flag_key = $1""",
                                   p["flag_key"]) if p["flag_key"] else None
        prs = await conn.fetch("""SELECT provider, repo, number, url, branch, state, created_at, merged_at, merge_sha
                                    FROM cleanup_pull_requests WHERE plan_id = $1 ORDER BY created_at""", p["id"])
        scream = await ox.scream_evidence(conn, p) if (p["canary"] or {}).get("fully_off_at") else None
    return {**row(p), "audit_log": [row(x) for x in log], "flag": row(flag) if flag else None,
            "pull_requests": [row(x) for x in prs], "scream_test": scream}


@router.post("/{plan_id}/evidence/{kind}")
async def generate_evidence(plan_id: str, kind: Literal["blast_radius", "impact_dossier", "cleanup_pr"],
                            who: Principal = Depends(require("read"))):
    check(who, EVIDENCE_PERMISSION[kind])
    org = who.org_id
    async with org_tx(org) as conn:
        manifest = await active_manifest(conn)
        p = await _load(conn, plan_id, for_update=True)
        spec = manifest.by_key().get(p["feature_key"])
        if spec is None:
            raise HTTPException(422, f"{p['feature_key']} is not in features.yaml")
        if kind == "cleanup_pr" and (p["cleanup_pr"] or {}).get("number"):
            raise HTTPException(409, "the cleanup PR is already open; its outline can no longer be regenerated")
        if kind in ("blast_radius", "cleanup_pr"):
            edges = [(r["from_path"], r["to_path"]) for r in await conn.fetch("SELECT from_path, to_path FROM import_edges")]
            br = blast_radius(spec, manifest, edges)
            if kind == "blast_radius":
                value = br
            else:
                value = cleanup_pr_outline(spec, p["blast_radius"] or br, await ox.known_repo_files(conn))
                value["branch"] = f"sunset/{spec.key}"
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
        await ox.audit(conn, org, p["id"], p["state"], p["state"], "note", {"evidence": kind}, who)
    return value


class Ack(BaseModel):
    kind: Literal["inbound", "account"]
    ids: list[str] = Field(min_length=1)


@router.post("/{plan_id}/acknowledge")
async def acknowledge(plan_id: str, body: Ack, who: Principal = Depends(require("read"))):
    """inbound: an owner acknowledges a blast-radius reference (PM / tech lead).
    account: Customer Success marks an account as contacted (CS lead)."""
    check(who, ACK_PERMISSION[body.kind])
    org = who.org_id
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        col, list_key, id_key = (("blast_radius", "inbound_references", "from_path") if body.kind == "inbound"
                                 else ("impact_dossier", "accounts", "account_id"))
        doc = p[col]
        if not doc:
            raise HTTPException(409, f"generate {col} first")
        wanted = set(body.ids)
        stamp = datetime.now(timezone.utc).isoformat()
        for item in doc[list_key]:
            if "*" in wanted or item[id_key] in wanted:
                item["acknowledged"] = True
                item.setdefault("acknowledged_at", stamp)
                item.setdefault("acknowledged_by_role", who.role.value)
        await conn.execute(f"UPDATE sunset_plans SET {col} = $2, version = version + 1 WHERE id = $1", p["id"], doc)
        await ox.audit(conn, org, p["id"], p["state"], p["state"], "note",
                       {"acknowledged": body.kind, "ids": body.ids}, who)
        return row(await _load(conn, plan_id))


class AdvanceIn(BaseModel):
    expected_version: int | None = None
    cs_signoff: bool = False


def advance_permission(p) -> str:
    state = SunsetState(p["state"])
    if state is SunsetState.CANARY_DARK_LAUNCH and p["canary_phase"] == "zero_usage_off":
        return CANARY_PROMOTE_PERMISSION
    return ADVANCE_PERMISSION.get(state, "sunset:approve_cleanup")


@router.post("/{plan_id}/advance")
async def advance_plan(plan_id: str, body: AdvanceIn, who: Principal = Depends(require("read"))):
    """One guarded step forward. Who may take it depends on the step (RBAC matrix in app/auth/rbac.py);
    canary steps drive the live flag, the canary exit opens the cleanup PR. Archiving normally happens
    through the PR merge webhook; an admin may also archive after merging by hand."""
    org = who.org_id
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        check(who, advance_permission(p))
        if body.cs_signoff:
            check(who, "sunset:cs_signoff")
        _check_version(p, body.expected_version)
        try:
            pr_merged = SunsetState(p["state"]) is SunsetState.DEAD_CODE_PR_OPENED and \
                (p["cleanup_pr"] or {}).get("state") == "merged"
            ctx, scream = await ox.gather_context(conn, org, p, {"cs_signoff": body.cs_signoff, "pr_merged": pr_merged})
            res = advance(ox.view(p), ctx)
            note = await ox.execute(conn, org, p, res, who, scream=scream)
        except (TransitionError, ox.ExecutionError) as e:
            raise _http(e) from e
        return row(await ox.persist(conn, org, p, res, {"cs_signoff": body.cs_signoff, **note}, who))


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/{plan_id}/rollback")
async def rollback_plan(plan_id: str, body: ReasonIn, who: Principal = Depends(require("sunset:rollback"))):
    """Re-enable the flag (fully_on), close an open cleanup PR, return to customer_outreach, rollback_count + 1."""
    org = who.org_id
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        try:
            res = rollback(ox.view(p), body.reason)
            note = await ox.execute(conn, org, p, res, who, reason=body.reason)
        except (TransitionError, ox.ExecutionError) as e:
            raise _http(e) from e
        return row(await ox.persist(conn, org, p, res, {"reason": body.reason, "rollback_number":
                                                         p["rollback_count"] + 1, **note}, who))


@router.post("/{plan_id}/cancel")
async def cancel_plan(plan_id: str, body: ReasonIn, who: Principal = Depends(require("sunset:cancel"))):
    org = who.org_id
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id, for_update=True)
        try:
            res = cancel(ox.view(p), body.reason)
            note = await ox.execute(conn, org, p, res, who, reason=body.reason)
        except (TransitionError, ox.ExecutionError) as e:
            raise _http(e) from e
        return row(await ox.persist(conn, org, p, res, {"reason": body.reason, **note}, who))


@router.get("/{plan_id}/scream-test")
async def scream_test(plan_id: str, org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        p = await _load(conn, plan_id)
        if not (p["canary"] or {}).get("fully_off_at"):
            raise HTTPException(409, "the scream test has not started (flag is not fully off)")
        ev = await ox.scream_evidence(conn, p)
        buckets = await conn.fetch("""SELECT date_trunc('day', bucket_start) AS day, sum(on_count)::bigint AS on_count,
                                             sum(off_count)::bigint AS off_count FROM flag_evaluations
                                       WHERE flag_key = $1 AND bucket_start >= $2 GROUP BY 1 ORDER BY 1""",
                                   p["flag_key"], datetime.fromisoformat(p["canary"]["fully_off_at"]))
    return {**ev, "daily": [row(b) for b in buckets],
            "passes": ev["quiet_days"] >= ev["required_days"] and not ev["flag_on_evaluations"]
            and not ev["tickets_since_off"]}


@router.post("/{plan_id}/cleanup-pr/simulate-merge")
async def simulate_merge(plan_id: str, who: Principal = Depends(require("flags:simulate"))):
    """SANDBOX_MODE: deliver the same GitHub `pull_request` (closed, merged) webhook GitHub would send."""
    if not config.sandbox_mode:
        raise HTTPException(404, "only available with SANDBOX_MODE=true; merge the PR on GitHub instead")
    async with org_tx(who.org_id) as conn:
        p = await _load(conn, plan_id)
        pr = p["cleanup_pr"] or {}
        if not pr.get("number"):
            raise HTTPException(409, "no cleanup PR is open for this plan")
        out = await ox.handle_pr_merged(conn, who.org_id, pr["repo"], int(pr["number"]),
                                        f"sandbox{int(pr['number']):04d}" + "0" * 32,
                                        datetime.now(timezone.utc).isoformat())
    return out
