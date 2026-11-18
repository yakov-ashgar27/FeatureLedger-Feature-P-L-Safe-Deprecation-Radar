"""GitHub ingester (REST v3): incremental, idempotent, rate-limit aware, identity-free.

Flow per repository
  1. List closed PRs sorted by `updated` desc; stop once `updated_at` <= stored cursor.
  2. For merged PRs: fetch files (paginated, max 3,000 per GitHub) and reviews.
  3. Scrub (allowlist) -> normalize -> envelope with idempotency key -> inbox upsert.
  4. Upsert `work_items` + `work_item_allocations` from the manifest; advance the cursor last.
Network I/O lives in `GitHubClient`; everything else is pure and unit-tested.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

from ..core.effort import raw_effort_hours
from ..core.manifest import Manifest
from ..core.types import WorkKind
from .scrub import scrub_files, scrub_pr, scrub_reviews

SWEEPING_MIN_FEATURES = 50
SWEEPING_MIN_LINES = 5_000
API = "https://api.github.com"


# ------------------------------------------------------------------ pure helpers
def idempotency_key(integration_id: str, entity_type: str, external_id: str, version: str) -> str:
    return hashlib.sha256(f"{integration_id}|{entity_type}|{external_id}|{version}".encode()).hexdigest()


def backoff_seconds(attempt: int, retry_after: str | None = None, reset_epoch: str | None = None,
                    now: float | None = None, base: float = 1.0, cap: float = 600.0,
                    rng: random.Random | None = None) -> float:
    """Server hints win (Retry-After, then X-RateLimit-Reset); else exponential backoff, full jitter."""
    now = time.time() if now is None else now
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), cap)
    if reset_epoch and reset_epoch.isdigit():
        return min(max(float(reset_epoch) - now, 0.0) + 1.0, cap)
    r = rng or random
    return r.uniform(0, min(cap, base * (2 ** attempt)))


def classify_kind(labels: list[str], title: str) -> tuple[WorkKind, str | None]:
    lab = {lbl.lower() for lbl in labels}
    t = title.lower().strip()
    priority = next((p.upper() for p in ("p0", "p1", "p2", "p3") if p in lab or f"sev-{p[1]}" in lab), None)
    if t.startswith('revert "') or "revert" in lab:
        return WorkKind.REVERT, priority
    if "incident" in lab:
        return WorkKind.INCIDENT, priority
    if "hotfix" in lab or t.startswith("hotfix"):
        return WorkKind.HOTFIX, priority
    if lab & {"bug", "bugfix", "fix"} or t.startswith(("fix", "bug")):
        return WorkKind.BUG, priority
    if lab & {"chore", "dependencies", "refactor", "docs", "ci"} or t.startswith(("chore", "refactor", "build", "ci", "docs")):
        return WorkKind.CHORE, priority
    return WorkKind.FEATURE, priority


@dataclass(frozen=True)
class NormalizedPR:
    external_id: str
    title_for_triage: str
    kind: WorkKind
    priority: str | None
    opened_at: str
    merged_at: str | None
    additions: int
    deletions: int
    files_changed: int
    review_rounds: int
    is_revert: bool
    is_sweeping: bool
    labels: list[str]
    linked_keys: list[str]
    path_stats: list[dict[str, Any]]
    allocations: dict[str, float]          # feature key -> share
    owner_team_key: str | None
    raw_effort_hours: float


def normalize_pr(repo: str, pr: dict[str, Any], files: list[dict[str, Any]], reviews: list[dict[str, Any]],
                 manifest: Manifest) -> NormalizedPR:
    """`pr`, `files`, `reviews` may be raw GitHub payloads; they are scrubbed here first."""
    p = scrub_pr(pr)
    fs = scrub_files(files)
    rv = scrub_reviews(reviews)
    kind, prio = classify_kind(p["labels"], p.get("title", ""))
    path_stats = [{"path": f["filename"], "additions": int(f.get("additions", 0)),
                   "deletions": int(f.get("deletions", 0))} for f in fs]
    counted = [s for s in path_stats if not manifest.is_ignored(s["path"])]
    adds = sum(s["additions"] for s in counted)
    dels = sum(s["deletions"] for s in counted)
    shares, _ = manifest.allocate_paths(path_stats)
    sweeping = len(shares) > SWEEPING_MIN_FEATURES or adds + dels > SWEEPING_MIN_LINES
    owner_team = None
    if shares:
        top = max(shares.items(), key=lambda kv: kv[1])[0]
        owner_team = manifest.by_key()[top].owner_team
    return NormalizedPR(
        external_id=f"{repo}#{p['number']}",
        title_for_triage=f"{repo}#{p['number']} · {p.get('title', '')[:120]}",
        kind=kind, priority=prio, opened_at=p["created_at"], merged_at=p.get("merged_at"),
        additions=adds, deletions=dels, files_changed=len(counted),
        review_rounds=rv["changes_requested"], is_revert=kind is WorkKind.REVERT, is_sweeping=sweeping,
        labels=p["labels"], linked_keys=p["linked_keys"], path_stats=path_stats, allocations=shares,
        owner_team_key=owner_team,
        raw_effort_hours=round(raw_effort_hours(adds, dels, len(counted), rv["changes_requested"], kind, prio), 2),
    )


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.split(";")
        if len(seg) >= 2 and 'rel="next"' in seg[1]:
            return seg[0].strip()[1:-1]
    return None


# ------------------------------------------------------------------ network client
class GitHubClient:
    """Thin async client. Requires `httpx` (installed in the Docker image)."""

    def __init__(self, token: str, max_attempts: int = 6):
        import httpx  # local import keeps the pure helpers importable without httpx
        self._http = httpx.AsyncClient(base_url=API, timeout=30.0, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "featureledger/0.1"})
        self.max_attempts = max_attempts

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None):
        for attempt in range(self.max_attempts):
            resp = await self._http.get(url, params=params)
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if resp.status_code in (403, 429) and (remaining == "0" or "Retry-After" in resp.headers):
                await asyncio.sleep(backoff_seconds(attempt, resp.headers.get("Retry-After"),
                                                    resp.headers.get("X-RateLimit-Reset")))
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(backoff_seconds(attempt))
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"GitHub GET {url} failed after {self.max_attempts} attempts")

    async def paginate(self, url: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        next_url: str | None = url
        while next_url:
            resp = await self._get(next_url, params)
            params = None  # the next link already carries the query
            for item in resp.json():
                yield item
            next_url = parse_next_link(resp.headers.get("Link"))

    async def updated_prs(self, repo: str, since_iso: str | None) -> AsyncIterator[dict[str, Any]]:
        async for pr in self.paginate(f"/repos/{repo}/pulls",
                                      {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100}):
            if since_iso and pr["updated_at"] <= since_iso:
                return
            yield pr

    async def pr_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [f async for f in self.paginate(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100})]

    async def pr_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [r async for r in self.paginate(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100})]


# ------------------------------------------------------------------ persistence
async def sync_repository(conn, org_id: str, integration_id: str, repo: str, client: GitHubClient,
                          manifest: Manifest, default_team_key: str | None = None) -> dict[str, int]:
    """Run inside an org-scoped transaction (app.db.org_tx). Returns counters."""
    entity = f"pull_request:{repo}"
    cursor = await conn.fetchval(
        "SELECT cursor FROM sync_cursors WHERE integration_id = $1 AND entity_type = $2", integration_id, entity)
    repo_id = await conn.fetchval(
        """INSERT INTO repositories (org_id, integration_id, full_name) VALUES ($1, $2, $3)
           ON CONFLICT (org_id, full_name) DO UPDATE SET integration_id = EXCLUDED.integration_id
           RETURNING id""", org_id, integration_id, repo)
    teams = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM teams")}
    feature_ids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
    stats = {"seen": 0, "merged": 0, "duplicates": 0}
    newest = cursor
    async for pr in client.updated_prs(repo, cursor):
        stats["seen"] += 1
        newest = max(newest or "", pr["updated_at"])
        if not pr.get("merged_at"):
            continue
        files = await client.pr_files(repo, pr["number"])
        reviews = await client.pr_reviews(repo, pr["number"])
        n = normalize_pr(repo, pr, files, reviews, manifest)
        key = idempotency_key(integration_id, "pull_request", n.external_id, pr["updated_at"])
        inserted = await conn.fetchval(
            """INSERT INTO ingest_inbox (idempotency_key, org_id, integration_id, entity_type, external_id, payload,
                                         processed_at)
               VALUES ($1, $2, $3, 'pull_request', $4, $5, now())
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING 1""",
            key, org_id, integration_id, n.external_id, json.loads(json.dumps(n.__dict__, default=str)))
        if not inserted:
            stats["duplicates"] += 1
            continue
        await upsert_work_item(conn, org_id, repo_id, n, teams.get(n.owner_team_key or default_team_key or ""),
                               feature_ids)
        stats["merged"] += 1
    if newest and newest != cursor:
        await conn.execute(
            """INSERT INTO sync_cursors (integration_id, org_id, entity_type, cursor) VALUES ($1, $2, $3, $4)
               ON CONFLICT (integration_id, entity_type) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()""",
            integration_id, org_id, entity, newest)
    await conn.execute("UPDATE integrations SET last_synced_at = now(), status = 'active', last_error = NULL "
                       "WHERE id = $1", integration_id)
    return stats


async def upsert_work_item(conn, org_id: str, repo_id, n: NormalizedPR, team_id, feature_ids: dict[str, Any]) -> None:
    wid = await conn.fetchval(
        """INSERT INTO work_items (org_id, source, external_id, item_type, kind, priority, team_id, repo_id,
                                   opened_at, merged_at, additions, deletions, files_changed, review_rounds,
                                   is_revert, is_sweeping, raw_effort_hours, path_stats, linked_keys, labels)
           VALUES ($1, 'github', $2, 'pr', $3::work_kind, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                   $16, $17, $18)
           ON CONFLICT (org_id, source, external_id) DO UPDATE SET
               kind = EXCLUDED.kind, priority = EXCLUDED.priority, team_id = EXCLUDED.team_id,
               merged_at = EXCLUDED.merged_at, additions = EXCLUDED.additions, deletions = EXCLUDED.deletions,
               files_changed = EXCLUDED.files_changed, review_rounds = EXCLUDED.review_rounds,
               is_revert = EXCLUDED.is_revert, is_sweeping = EXCLUDED.is_sweeping,
               raw_effort_hours = EXCLUDED.raw_effort_hours, path_stats = EXCLUDED.path_stats,
               linked_keys = EXCLUDED.linked_keys, labels = EXCLUDED.labels
           RETURNING id""",
        org_id, n.external_id, n.kind.value, n.priority, team_id, repo_id,
        datetime.fromisoformat(n.opened_at.replace("Z", "+00:00")),
        datetime.fromisoformat(n.merged_at.replace("Z", "+00:00")) if n.merged_at else None,
        n.additions, n.deletions, n.files_changed, n.review_rounds, n.is_revert, n.is_sweeping,
        n.raw_effort_hours, n.path_stats, n.linked_keys, n.labels)
    await conn.execute("DELETE FROM work_item_allocations WHERE work_item_id = $1", wid)
    rows = [(wid, feature_ids[k], org_id, share) for k, share in n.allocations.items() if k in feature_ids]
    if rows:
        await conn.executemany(
            "INSERT INTO work_item_allocations (work_item_id, feature_id, org_id, share) VALUES ($1, $2, $3, $4)", rows)
    if not n.allocations and not n.is_sweeping:
        await conn.execute(
            """INSERT INTO mapping_suggestions (org_id, subject_type, subject_id, subject_label, suggested_feature_id,
                                                confidence, source, rationale)
               VALUES ($1, 'work_item', $2, $3, NULL, 0, 'heuristic', 'No manifest path matched any changed file')
               ON CONFLICT (org_id, subject_type, subject_id) DO NOTHING""", org_id, wid, n.title_for_triage)
