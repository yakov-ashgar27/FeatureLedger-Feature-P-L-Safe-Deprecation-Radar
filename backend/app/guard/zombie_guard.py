"""Zombie-Guard: react to `pull_request` opened / synchronize / reopened.

    changed paths -> Feature Graph owner (human > manifest > auto-confirmed) -> features that are zombies,
    in a live sunset plan, or archived -> one PR comment with trailing 3-month net P&L, adoption and the
    sunset stage. Never blocks: no status check, no review request, just context before review time is spent.

Idempotent: one comment per PR, edited in place on every push (`synchronize`); identical bodies are not
re-sent. The cleanup PR FeatureLedger opened itself (branch sunset/*) is ignored.
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import config
from ..graph.manifest_store import active_manifest
from ..graph.reallocate import load_resolver
from .providers import MARKER, CommentProvider

log = logging.getLogger("featureledger.guard")
GUARD_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
STAGE = {
    "candidate_for_sunset": "Sunset candidate: blast radius under review",
    "blast_radius_checked": "Sunset: customer impact being assessed",
    "customer_outreach": "Sunset: customers are being contacted",
    "canary_dark_launch": "Sunset: flag being turned off (dark launch)",
    "dead_code_pr_opened": "Sunset: cleanup PR open, code about to be deleted",
}


@dataclass
class FeatureImpact:
    key: str
    name: str
    status: str
    classification: str | None
    net_3m: float | None
    adoption: float | None
    gross_margin_pct: float | None
    margin_bleed: bool
    eng_hours_month: float | None
    sunset_state: str | None
    canary_phase: str | None
    scream_day: int | None
    scream_days: int | None
    cleanup_branch: str | None
    files: int

    @property
    def flagged(self) -> bool:
        return self.classification == "zombie" or self.sunset_state is not None or self.status == "archived"

    def stage(self) -> str:
        if self.status == "archived":
            return "Archived: this feature was removed"
        if not self.sunset_state:
            return "Zombie, no sunset plan yet" if self.classification == "zombie" else "–"
        s = STAGE.get(self.sunset_state, self.sunset_state)
        if self.sunset_state == "canary_dark_launch":
            if self.canary_phase == "fully_off" and self.scream_day is not None:
                s = f"Sunset: flag fully off, scream test day {self.scream_day}/{self.scream_days}"
            elif self.canary_phase == "zero_usage_off":
                s = "Sunset: flag off for accounts not using it"
        return s


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    sign = "−" if v < 0 else "+"
    return f"{sign}${abs(v):,.0f}"


def render(impacts: list[FeatureImpact], *, head_sha: str | None, total_files: int, other_features: int) -> str:
    flagged = [i for i in impacts if i.flagged]
    sha = (head_sha or "")[:7]
    foot = (f"<sub>FeatureLedger Zombie-Guard · {total_files} changed file(s)"
            f"{f' at `{sha}`' if sha else ''} · P&L is the trailing 3-month total · this comment never blocks a merge "
            f"and updates itself on every push</sub>")
    if not flagged:
        return "\n".join([MARKER, "### ✅ FeatureLedger Zombie-Guard",
                          "This PR no longer touches a zombie or retiring feature.", "", foot])
    lines = [MARKER, "### 🧟 FeatureLedger Zombie-Guard",
             f"This PR changes **{len(flagged)}** feature(s) that are being retired or do not pay for themselves. "
             "Nothing is blocked; this is context for the author and reviewers before more time goes in.", "",
             "| Feature | Health | Net P&L (3 mo) | Adoption | Stage | Files |", "|---|---|---|---|---|---:|"]
    for i in flagged:
        link = f"[{i.name}]({config.app_url}/features/{i.key})"
        health = "Archived" if i.status == "archived" else (i.classification or "–").replace("_", " ").title()
        if i.margin_bleed:
            health += " · margin bleed"
        adoption = f"{i.adoption:.1%}" if i.adoption is not None else "n/a"
        lines.append(f"| {link} | {health} | {_money(i.net_3m)} | {adoption} | {i.stage()} | {i.files} |")
    lines += ["", "**Before you merge**"]
    for i in flagged:
        if i.status == "archived":
            lines.append(f"- `{i.key}` is archived. If this code still exists it should go, not grow.")
        elif i.sunset_state == "dead_code_pr_opened":
            lines.append(f"- `{i.key}`: the cleanup PR on `{i.cleanup_branch or 'sunset/' + i.key}` deletes this "
                         "code. Changes here will be thrown away; coordinate with it instead.")
        elif i.sunset_state:
            lines.append(f"- `{i.key}` is being sunset. Prefer fixes that help customers migrate over new behaviour.")
        else:
            hours = f" (~{i.eng_hours_month:,.0f} engineering h/mo today)" if i.eng_hours_month else ""
            lines.append(f"- `{i.key}` costs more than it earns{hours}. Is this change needed, or should it start "
                         "a sunset plan instead?")
    if other_features:
        lines += ["", f"Also touches {other_features} healthy feature(s)."]
    lines += ["", foot]
    return "\n".join(lines)


async def assess(conn, paths: list[str]) -> tuple[list[FeatureImpact], int]:
    """-> impacts for every touched feature (flagged or not), unmapped file count."""
    resolver = await load_resolver(conn, await active_manifest(conn))
    counts: Counter[str] = Counter()
    unmapped = 0
    for p in paths:
        key, _ = resolver.owner(p)
        if key:
            counts[key] += 1
        else:
            unmapped += 1
    if not counts:
        return [], unmapped
    rows = await conn.fetch("""
        WITH latest AS (SELECT max(period_month) AS m FROM feature_pnl_monthly),
        win AS (SELECT p.feature_id, sum(p.v_usage - p.c_total) AS net_3m
                  FROM feature_pnl_monthly p, latest
                 WHERE p.period_month > latest.m - interval '3 months' GROUP BY p.feature_id)
        SELECT f.key, f.name, f.status::text AS status, cur.classification::text AS classification,
               win.net_3m, cur.active_accounts, cur.eligible_accounts, cur.gross_margin_pct, cur.margin_bleed,
               cur.eng_hours, sp.state::text AS sunset_state, sp.canary_phase, sp.canary, sp.scream_test_days,
               sp.cleanup_pr
          FROM features f
          LEFT JOIN latest ON true
          LEFT JOIN feature_pnl_monthly cur ON cur.feature_id = f.id AND cur.period_month = latest.m
          LEFT JOIN win ON win.feature_id = f.id
          LEFT JOIN sunset_plans sp ON sp.feature_id = f.id AND sp.state NOT IN ('archived', 'cancelled')
         WHERE f.key = ANY($1::text[])""", list(counts))
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        canary = r["canary"] or {}
        day = None
        if canary.get("fully_off_at"):
            day = (now - datetime.fromisoformat(canary["fully_off_at"])).days
        out.append(FeatureImpact(
            key=r["key"], name=r["name"], status=r["status"], classification=r["classification"],
            net_3m=float(r["net_3m"]) if r["net_3m"] is not None else None,
            adoption=(r["active_accounts"] / r["eligible_accounts"]) if r["eligible_accounts"] else None,
            gross_margin_pct=float(r["gross_margin_pct"]) if r["gross_margin_pct"] is not None else None,
            margin_bleed=bool(r["margin_bleed"]), eng_hours_month=float(r["eng_hours"]) if r["eng_hours"] else None,
            sunset_state=r["sunset_state"], canary_phase=r["canary_phase"], scream_day=day,
            scream_days=r["scream_test_days"], cleanup_branch=(r["cleanup_pr"] or {}).get("branch"),
            files=counts[r["key"]]))
    out.sort(key=lambda i: (not i.flagged, i.net_3m if i.net_3m is not None else 0))
    return out, unmapped


def body_sha(body: str) -> str:
    # the footer carries the head sha; compare content without it so a push with no P&L change is a no-op
    core = "\n".join(ln for ln in body.splitlines() if not ln.startswith("<sub>"))
    return hashlib.sha256(core.encode()).hexdigest()


async def preview(conn, paths: list[str], head_sha: str | None = None) -> dict[str, Any]:
    impacts, unmapped = await assess(conn, paths)
    flagged = [i for i in impacts if i.flagged]
    body = render(impacts, head_sha=head_sha, total_files=len(paths),
                  other_features=len(impacts) - len(flagged)) if flagged else None
    return {"flagged": bool(flagged), "features": [{**asdict(i), "flagged": i.flagged, "stage": i.stage()}
                                                   for i in impacts],
            "unmapped_files": unmapped, "comment_markdown": body}


async def handle_pull_request(conn, org: str, payload: dict[str, Any], comments: CommentProvider,
                              paths: list[str]) -> dict[str, Any]:
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name") or ""
    number = int(payload.get("number") or pr.get("number") or 0)
    head = pr.get("head") or {}
    if head.get("ref", "").startswith("sunset/") or await conn.fetchval(
            "SELECT 1 FROM cleanup_pull_requests WHERE repo = $1 AND number = $2", repo, number):
        return {"zombie_guard": "ignored", "reason": "FeatureLedger's own cleanup PR"}
    impacts, _ = await assess(conn, paths)
    flagged = [i for i in impacts if i.flagged]
    existing = await conn.fetchrow("SELECT * FROM pr_guard_comments WHERE repo = $1 AND pr_number = $2 FOR UPDATE",
                                   repo, number)
    if not flagged and not existing:
        return {"zombie_guard": "clean", "features": [i.key for i in impacts]}
    body = render(impacts, head_sha=head.get("sha"), total_files=len(paths),
                  other_features=len(impacts) - len(flagged))
    sha = body_sha(body)
    feats = [i.key for i in flagged]
    if existing and existing["body_sha"] == sha:
        await conn.execute("""UPDATE pr_guard_comments SET head_sha = $3 WHERE repo = $1 AND pr_number = $2""",
                           repo, number, head.get("sha"))
        return {"zombie_guard": "unchanged", "comment_id": existing["comment_id"], "features": feats}
    if existing:
        await comments.update(repo, int(existing["comment_id"]), body)
        await conn.execute("""UPDATE pr_guard_comments SET body = $3, body_sha = $4, features = $5, flagged = $6,
                                     head_sha = $7, updates = updates + 1, updated_at = now()
                               WHERE repo = $1 AND pr_number = $2""",
                           repo, number, body, sha, feats, bool(flagged), head.get("sha"))
        return {"zombie_guard": "updated", "comment_id": existing["comment_id"], "features": feats}
    cid = await comments.find(repo, number)
    if cid is not None:
        await comments.update(repo, cid, body)
    else:
        cid = await comments.create(repo, number, body)
    await conn.execute("""INSERT INTO pr_guard_comments (org_id, repo, pr_number, provider, comment_id, head_sha, body,
                                                         body_sha, features, flagged)
                          VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                       org, repo, number, comments.name, cid, head.get("sha"), body, sha, feats, bool(flagged))
    log.info("zombie-guard commented on %s#%s: %s", repo, number, feats)
    return {"zombie_guard": "created", "comment_id": cid, "features": feats}
