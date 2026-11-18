"""Safe Sunset execution: the side effects behind each guarded transition.

The pure state machine (app/core/sunset.py) decides *whether* a plan may move. This module makes the
move real, inside the same database transaction as the state change, and fails closed: if a flag
provider or GitHub call fails, the transaction rolls back and the plan stays where it was.

    customer_outreach  -> canary_dark_launch   flag -> zero_usage_off (on only for accounts with usage in 90 d)
    canary (promote)                            flag -> fully_off; the scream-test clock starts
    canary_dark_launch -> dead_code_pr_opened   evaluations re-pulled, guards re-checked, cleanup PR opened
    dead_code_pr_opened -> archived             PR merge webhook (or an admin); feature archived
    rollback / cancel from canary or PR         flag -> fully_on, cleanup PR closed, rollback_count + 1
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..auth.session import Principal
from ..cleanup.change import build_cleanup_change
from ..cleanup.github import CleanupPRProvider, CleanupProviderError, get_cleanup_provider
from ..config import config
from ..core.manifest import ManifestError, parse_manifest
from ..core.sunset import ROLLBACK_FROM, PlanView, SunsetState, TransitionResult, advance
from ..core.sunset_artifacts import blast_radius
from ..flags.base import EvalBucket, FlagProvider, FlagProviderError, FlagState
from ..flags.registry import get_flag_provider
from ..graph.manifest_store import active_manifest_text

log = logging.getLogger("featureledger.sunset")

PLAN_SQL = """
    SELECT p.*, f.key AS feature_key, f.name AS feature_name, f.flag_key, f.is_regulatory, f.is_shared_overhead,
           f.metadata AS feature_metadata
      FROM sunset_plans p JOIN features f ON f.id = p.feature_id"""


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.status = code, status


class PlanNotFound(LookupError):
    pass


def view(p) -> PlanView:
    return PlanView(state=SunsetState(p["state"]), feature_is_regulatory=p["is_regulatory"], flag_key=p["flag_key"],
                    scream_test_days=p["scream_test_days"], state_entered_at=p["state_entered_at"],
                    canary_phase=p["canary_phase"], blast_radius=p["blast_radius"], impact_dossier=p["impact_dossier"],
                    canary=p["canary"], cleanup_pr=p["cleanup_pr"])


def actor_fields(actor: Principal | None, system: str | None = None) -> dict[str, Any]:
    if actor is not None:
        return actor.audit()
    return {"actor": system or "system", "actor_role": "system", "actor_kind": "system"}


async def audit(conn, org: str, plan_id, frm: str | None, to: str, action: str, payload: dict[str, Any],
                actor: Principal | None = None, system: str | None = None) -> None:
    await conn.execute("""INSERT INTO sunset_audit_logs (org_id, plan_id, from_state, to_state, action, actor_id, payload)
                          VALUES ($1, $2, $3::sunset_state, $4::sunset_state, $5, $6::uuid, $7)""",
                       org, plan_id, frm, to, action, actor.user_id if actor else None,
                       {**payload, **actor_fields(actor, system)})


async def load(conn, plan_id, for_update: bool = False):
    try:
        p = await conn.fetchrow(PLAN_SQL + " WHERE p.id = $1::uuid" + (" FOR UPDATE OF p" if for_update else ""),
                                str(plan_id))
    except Exception as e:   # malformed uuid
        if "invalid input syntax for type uuid" in str(e):
            raise PlanNotFound(plan_id) from e
        raise
    if not p:
        raise PlanNotFound(plan_id)
    return p


async def persist(conn, org: str, p, res: TransitionResult, note: dict[str, Any], actor: Principal | None,
                  system: str | None = None):
    """Write the transition + its audit row. `res.updates` already carries execution results."""
    u = res.updates
    entered = datetime.now(timezone.utc) if res.to_state != res.from_state or res.action == "canary_promote" \
        else p["state_entered_at"]
    await conn.execute("""
        UPDATE sunset_plans SET state = $2::sunset_state, canary_phase = $3,
               blast_radius = coalesce($4, blast_radius), impact_dossier = coalesce($5, impact_dossier),
               canary = coalesce($6, canary), cleanup_pr = coalesce($7, cleanup_pr),
               state_entered_at = $8, updated_at = now(), version = version + 1,
               rollback_count = rollback_count + $9
         WHERE id = $1""", p["id"], res.to_state.value,
        u["canary_phase"] if "canary_phase" in u else p["canary_phase"],
        u.get("blast_radius"), u.get("impact_dossier"), u.get("canary"), u.get("cleanup_pr"), entered,
        1 if res.action == "rollback" else 0)
    if res.to_state is SunsetState.ARCHIVED:
        await conn.execute("UPDATE features SET status = 'archived', updated_at = now() WHERE id = $1", p["feature_id"])
    elif res.to_state is SunsetState.CANCELLED:
        await conn.execute("UPDATE features SET status = 'active', updated_at = now() WHERE id = $1", p["feature_id"])
    elif res.to_state in (SunsetState.CANARY_DARK_LAUNCH, SunsetState.DEAD_CODE_PR_OPENED):
        await conn.execute("UPDATE features SET status = 'sunsetting' WHERE id = $1", p["feature_id"])
    elif res.action == "rollback":
        await conn.execute("UPDATE features SET status = 'candidate_for_sunset' WHERE id = $1", p["feature_id"])
    await audit(conn, org, p["id"], res.from_state.value, res.to_state.value, res.action,
                {**note, **({"flag_action": u["flag_action"]} if "flag_action" in u else {})}, actor, system)
    return await load(conn, p["id"])


# ------------------------------------------------------------------------------------- flags
async def keep_on_accounts(conn, feature_id, now: datetime) -> list[str]:
    """Accounts with at least one event on the feature in the last 90 days keep the flag on."""
    key = "a.id::text" if config.flag_account_key == "id" else "coalesce(a.external_ref, a.id::text)"
    rows = await conn.fetch(f"""
        SELECT DISTINCT {key} AS k FROM usage_signals u JOIN accounts a ON a.id = u.account_id
         WHERE u.feature_id = $1 AND u.events > 0
           AND u.period_month >= date_trunc('month', $2::timestamptz - interval '90 days')::date
         ORDER BY 1""", feature_id, now)
    return [r["k"] for r in rows]


async def set_flag(conn, org: str, *, plan_id, flag_key: str, state: FlagState, keep: list[str],
                   actor: Principal | None, provider: FlagProvider, comment: str) -> dict[str, Any]:
    try:
        ref = await provider.set_state(flag_key, state, keep, comment)
    except FlagProviderError as e:
        raise ExecutionError("flag_provider_error", str(e)) from e
    except Exception as e:  # network
        raise ExecutionError("flag_provider_error", f"{provider.name}: {e}") from e
    await conn.execute("""
        INSERT INTO flag_states (org_id, flag_key, provider, state, keep_on_for, plan_id, provider_ref, last_error,
                                 updated_by, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, $8::uuid, now())
        ON CONFLICT (org_id, flag_key) DO UPDATE SET provider = EXCLUDED.provider, state = EXCLUDED.state,
               keep_on_for = EXCLUDED.keep_on_for, plan_id = EXCLUDED.plan_id, provider_ref = EXCLUDED.provider_ref,
               last_error = NULL, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        org, flag_key, provider.name, state.value, keep, plan_id, ref, actor.user_id if actor else None)
    log.info("flag %s -> %s via %s (%d kept on)", flag_key, state.value, provider.name, len(keep))
    return {"flag_state": state.value, "flag_provider": provider.name, "keep_on_for_count": len(keep)}


async def record_buckets(conn, org: str, flag_key: str, buckets: list[EvalBucket], source: str,
                         accumulate: bool = False) -> int:
    """Pulled counts (LaunchDarkly) replace a bucket; pushed batches (Statsig, sandbox) add to it."""
    agg = "flag_evaluations.{0} + EXCLUDED.{0}" if accumulate else "EXCLUDED.{0}"
    for b in buckets:
        await conn.execute(f"""
            INSERT INTO flag_evaluations (org_id, flag_key, bucket_start, source, on_count, off_count)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (org_id, flag_key, bucket_start, source) DO UPDATE
               SET on_count = {agg.format('on_count')}, off_count = {agg.format('off_count')}, ingested_at = now()""",
            org, flag_key, b.bucket_start, source, b.on_count, b.off_count)
    return len(buckets)


async def ingest_evaluations(conn, org: str, flag_key: str, since: datetime, provider: FlagProvider,
                             now: datetime | None = None) -> int:
    if provider.push_based:
        return 0
    now = now or datetime.now(timezone.utc)
    try:
        buckets = await provider.pull_evaluations(flag_key, since, now)
    except Exception as e:
        raise ExecutionError("flag_provider_error", f"cannot read evaluation counts from {provider.name}: {e}") from e
    return await record_buckets(conn, org, flag_key, buckets, provider.name)


async def scream_evidence(conn, p, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    canary = p["canary"] or {}
    off_at = datetime.fromisoformat(canary["fully_off_at"])
    ev = await conn.fetchrow("""SELECT coalesce(sum(on_count), 0)::bigint AS on_n, coalesce(sum(off_count), 0)::bigint AS off_n
                                  FROM flag_evaluations WHERE flag_key = $1 AND bucket_start >= $2""",
                             p["flag_key"], off_at)
    tickets = await conn.fetchval("SELECT count(*) FROM support_tickets WHERE feature_id = $1 AND created_at >= $2",
                                  p["feature_id"], off_at)
    return {"fully_off_at": off_at.isoformat(), "quiet_days": (now - off_at).days,
            "required_days": p["scream_test_days"], "flag_on_evaluations": int(ev["on_n"]),
            "flag_off_evaluations": int(ev["off_n"]), "tickets_since_off": int(tickets)}


# ------------------------------------------------------------------------------------- cleanup PR
async def known_repo_files(conn) -> list[str]:
    rows = await conn.fetch("""SELECT DISTINCT jsonb_array_elements(path_stats)->>'path' AS p FROM work_items
                               UNION SELECT from_path FROM import_edges UNION SELECT to_path FROM import_edges""")
    return sorted(r["p"] for r in rows if r["p"])


async def audit_rows(conn, plan_id) -> list[dict[str, Any]]:
    rows = await conn.fetch("""SELECT from_state::text, to_state::text, action, payload, created_at,
                                      encode(hash, 'hex') AS hash FROM sunset_audit_logs
                                WHERE plan_id = $1 ORDER BY id""", plan_id)
    return [dict(r) for r in rows]


async def open_cleanup_pr(conn, org: str, p, scream: dict[str, Any], gh: CleanupPRProvider) -> dict[str, Any]:
    try:
        repo_text = await gh.manifest_text("features.yaml")
        text = repo_text or await active_manifest_text(conn)
        try:
            manifest = parse_manifest(text)
        except ManifestError:
            text = await active_manifest_text(conn)
            manifest = parse_manifest(text)
        spec = manifest.by_key().get(p["feature_key"])
        if spec is None:   # already removed from the repo manifest: fall back to the active version
            text = await active_manifest_text(conn)
            manifest = parse_manifest(text)
            spec = manifest.by_key()[p["feature_key"]]
        files = await gh.list_files() or await known_repo_files(conn)
        plan = {"id": str(p["id"]), "reason": p["reason"], "rollback_count": p["rollback_count"]}
        br = p["blast_radius"]
        if not br:   # plans created before the blast radius was stored: compute it now for the PR body
            edges = [(r["from_path"], r["to_path"]) for r in await conn.fetch("SELECT from_path, to_path FROM import_edges")]
            br = blast_radius(spec, manifest, edges)
        change = build_cleanup_change(spec=spec, manifest=manifest, manifest_text=text, manifest_path="features.yaml",
                                      repo_files=files, blast_radius=br, plan=plan,
                                      audit=await audit_rows(conn, p["id"]), scream=scream)
        nxt = await conn.fetchval("SELECT coalesce(max(number), 1000) + 1 FROM cleanup_pull_requests WHERE repo = $1",
                                  gh.repo)
        pr = await gh.open_pr(change, next_number=int(nxt))
    except (CleanupProviderError, KeyError) as e:
        raise ExecutionError("cleanup_pr_error", str(e)) from e
    except ExecutionError:
        raise
    except Exception as e:  # network
        raise ExecutionError("cleanup_pr_error", f"GitHub: {e}") from e
    await conn.execute("""
        INSERT INTO cleanup_pull_requests (org_id, plan_id, provider, repo, number, url, branch, base_branch, head_sha,
                                           files_deleted, files_edited)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (org_id, repo, number) DO UPDATE SET head_sha = EXCLUDED.head_sha, state = 'open',
               files_deleted = EXCLUDED.files_deleted, files_edited = EXCLUDED.files_edited, plan_id = EXCLUDED.plan_id""",
        org, p["id"], pr.provider, pr.repo, pr.number, pr.url, pr.branch, pr.base_branch, pr.head_sha,
        pr.files_deleted, pr.files_edited)
    return {**pr.as_dict(), "title": change.title, "body_markdown": change.body, "state": "open",
            "opened_at": datetime.now(timezone.utc).isoformat(), "patch_path": change.patch_path,
            "manifest_edited": change.manifest_after is not None, "files_to_delete": pr.files_deleted,
            "files_to_edit": [c["path"] for c in change.patch["call_sites_to_edit"]],
            "routes_to_remove": change.patch["routes_to_remove"], "flag_key": p["flag_key"]}


async def close_cleanup_pr(conn, p, gh: CleanupPRProvider, reason: str) -> dict[str, Any] | None:
    pr = p["cleanup_pr"] or {}
    if not pr.get("number") or pr.get("state") != "open":
        return None
    closed = dict(pr, state="closed", closed_at=datetime.now(timezone.utc).isoformat())
    try:
        await gh.close_pr(int(pr["number"]), f"Closed by FeatureLedger: sunset rolled back / cancelled ({reason}). "
                                             "The feature flag is fully on again.")
    except Exception as e:   # never block a rollback on GitHub; record it instead
        log.warning("could not close cleanup PR #%s: %s", pr["number"], e)
        closed["close_error"] = str(e)[:300]
    await conn.execute("UPDATE cleanup_pull_requests SET state = 'closed' WHERE plan_id = $1 AND number = $2",
                       p["id"], int(pr["number"]))
    return closed


# ------------------------------------------------------------------------------------- dispatcher
async def gather_context(conn, org: str, p, body_ctx: dict[str, Any], provider: FlagProvider | None = None,
                         now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fresh evidence for the guards. In the scream test, evaluation counts are re-pulled first."""
    ctx: dict[str, Any] = {"blast_radius": p["blast_radius"], "impact_dossier": p["impact_dossier"],
                           "cleanup_pr": p["cleanup_pr"], **body_ctx}
    scream = None
    canary = p["canary"] or {}
    if p["state"] == SunsetState.CANARY_DARK_LAUNCH.value and canary.get("fully_off_at"):
        if p["flag_key"]:
            await ingest_evaluations(conn, org, p["flag_key"], datetime.fromisoformat(canary["fully_off_at"]),
                                     provider or get_flag_provider(), now)
        scream = await scream_evidence(conn, p, now)
        ctx["tickets_since_off"] = scream["tickets_since_off"]
        ctx["flag_on_evaluations"] = scream["flag_on_evaluations"]
        # the PR is generated on this transition; no hand-made outline is needed first
        ctx["cleanup_pr"] = p["cleanup_pr"] or {"generated_on_transition": True}
    return ctx, scream


async def execute(conn, org: str, p, res: TransitionResult, actor: Principal | None, *,
                  flags: FlagProvider | None = None, gh: CleanupPRProvider | None = None,
                  scream: dict[str, Any] | None = None, reason: str = "",
                  now: datetime | None = None) -> dict[str, Any]:
    """Perform the side effects of a validated transition; mutates res.updates; returns audit payload."""
    now = now or datetime.now(timezone.utc)
    note: dict[str, Any] = {}
    flag = p["flag_key"]
    who = actor.email if actor else "system"
    to = res.to_state
    if res.action == "transition" and to is SunsetState.CANARY_DARK_LAUNCH:
        keep = await keep_on_accounts(conn, p["feature_id"], now)
        info = await set_flag(conn, org, plan_id=p["id"], flag_key=flag, state=FlagState.ZERO_USAGE_OFF, keep=keep,
                              actor=actor, provider=flags or get_flag_provider(),
                              comment=f"FeatureLedger sunset {p['feature_key']}: dark launch (approved by {who})")
        res.updates["canary"] = {**res.updates["canary"], "keep_on_for_count": len(keep),
                                 "provider": info["flag_provider"]}
        note.update(info)
    elif res.action == "canary_promote":
        info = await set_flag(conn, org, plan_id=p["id"], flag_key=flag, state=FlagState.FULLY_OFF, keep=[],
                              actor=actor, provider=flags or get_flag_provider(),
                              comment=f"FeatureLedger sunset {p['feature_key']}: scream test starts (by {who})")
        note.update(info)
    elif res.action == "transition" and to is SunsetState.DEAD_CODE_PR_OPENED:
        pr = await open_cleanup_pr(conn, org, p, scream or {}, gh or get_cleanup_provider())
        res.updates["cleanup_pr"] = pr
        note.update({"cleanup_pr": {"provider": pr["provider"], "repo": pr["repo"], "number": pr["number"],
                                    "url": pr["url"], "branch": pr["branch"],
                                    "files_deleted": len(pr["files_deleted"])}, "scream_test": scream})
    elif res.action in ("rollback", "cancel") and SunsetState(p["state"]) in ROLLBACK_FROM:
        if flag:
            info = await set_flag(conn, org, plan_id=p["id"], flag_key=flag, state=FlagState.FULLY_ON, keep=[],
                                  actor=actor, provider=flags or get_flag_provider(),
                                  comment=f"FeatureLedger sunset {p['feature_key']}: {res.action} ({reason})")
            note.update(info)
        closed = await close_cleanup_pr(conn, p, gh or get_cleanup_provider(), reason) if p["cleanup_pr"] else None
        if closed:
            res.updates["cleanup_pr"] = closed
            note["cleanup_pr_closed"] = closed["number"]
    return note


async def handle_pr_merged(conn, org: str, repo: str, number: int, merge_sha: str | None,
                           merged_at: str | None) -> dict[str, Any]:
    """GitHub `pull_request` closed+merged on a sunset/<key> branch -> plan archived, feature archived."""
    pr = await conn.fetchrow("SELECT * FROM cleanup_pull_requests WHERE repo = $1 AND number = $2 FOR UPDATE",
                             repo, number)
    if not pr:
        return {"matched": False}
    if pr["state"] == "merged":
        return {"matched": True, "already_merged": True}
    try:
        merged_ts = datetime.fromisoformat(merged_at.replace("Z", "+00:00")) if merged_at else datetime.now(timezone.utc)
    except ValueError:
        merged_ts = datetime.now(timezone.utc)
    merged_at = merged_ts.isoformat()
    await conn.execute("""UPDATE cleanup_pull_requests SET state = 'merged', merged_at = $2, merge_sha = $3
                           WHERE id = $1""", pr["id"], merged_ts, merge_sha)
    p = await load(conn, pr["plan_id"], for_update=True)
    if p["state"] != SunsetState.DEAD_CODE_PR_OPENED.value:
        return {"matched": True, "plan_state": p["state"], "archived": False}
    res = advance(view(p), {"pr_merged": True})
    res.updates["cleanup_pr"] = {**(p["cleanup_pr"] or {}), "state": "merged", "merge_sha": merge_sha,
                                 "merged_at": merged_at or datetime.now(timezone.utc).isoformat()}
    await persist(conn, org, p, res, {"pr_merged": True, "repo": repo, "number": number, "merge_sha": merge_sha},
                  None, system="github-webhook")
    return {"matched": True, "plan_id": str(p["id"]), "feature": p["feature_key"], "archived": True}


async def scan_canaries(conn, org: str, provider: FlagProvider | None = None,
                        now: datetime | None = None) -> list[dict[str, Any]]:
    """Worker cron: pull evaluation counts for every plan in canary and raise a one-time 'scream_alert'
    audit note when 'on' evaluations or tickets appear during the scream test."""
    now = now or datetime.now(timezone.utc)
    provider = provider or get_flag_provider()
    out = []
    for p in await conn.fetch(PLAN_SQL + " WHERE p.state = 'canary_dark_launch'"):
        canary = p["canary"] or {}
        since = canary.get("fully_off_at") or canary.get("started_at")
        if not p["flag_key"] or not since:
            continue
        pulled = await ingest_evaluations(conn, org, p["flag_key"], datetime.fromisoformat(since) - timedelta(hours=1),
                                          provider, now)
        item = {"plan_id": str(p["id"]), "flag": p["flag_key"], "buckets": pulled}
        if canary.get("fully_off_at"):
            ev = await scream_evidence(conn, p, now)
            seen = canary.get("alerted", {})
            if (ev["flag_on_evaluations"], ev["tickets_since_off"]) != (seen.get("on", 0), seen.get("tickets", 0)) \
                    and (ev["flag_on_evaluations"] or ev["tickets_since_off"]):
                await conn.execute("UPDATE sunset_plans SET canary = canary || $2 WHERE id = $1", p["id"],
                                   {"alerted": {"on": ev["flag_on_evaluations"], "tickets": ev["tickets_since_off"]}})
                await audit(conn, org, p["id"], p["state"], p["state"], "note",
                            {"scream_alert": {k: ev[k] for k in ("flag_on_evaluations", "tickets_since_off")}},
                            None, system="flag-monitor")
                item["alert"] = True
            item.update(ev)
        out.append(item)
    return out
