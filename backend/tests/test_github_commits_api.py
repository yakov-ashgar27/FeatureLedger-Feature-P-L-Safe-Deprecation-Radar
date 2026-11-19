"""Direct pushes to the default branch become work items; merge commits and PR commits are skipped.
Real Postgres (RLS on, app role), a fake GitHub. CI runs it after the seed:

    FL_INTEGRATION=1 DATABASE_URL=... ADMIN_DATABASE_URL=... pytest tests/test_github_commits_api.py
"""
from __future__ import annotations

import asyncio
import os

import pytest

if os.environ.get("FL_INTEGRATION") != "1":
    pytest.skip("integration suite: set FL_INTEGRATION=1 with a migrated, seeded database", allow_module_level=True)

from app.config import config  # noqa: E402
from app.db import close_pool, org_tx, resolve_org  # noqa: E402
from app.graph.manifest_store import active_manifest  # noqa: E402
from app.ingest.github import sync_repository  # noqa: E402

REPO = "ci-probe/direct-push"


def commit(sha, when, msg, files, parents=1):
    return {"sha": sha, "parents": [{"sha": "p%d" % i} for i in range(parents)],
            "commit": {"message": msg, "committer": {"date": when}, "author": {"name": "Dev", "date": when}},
            "files": files}


class FakeGitHub:
    def __init__(self):
        self.commits = [
            commit("a" * 40, "2026-11-15T10:00:00Z", "fix(pdf): page break", [
                {"filename": "services/export/pdf_v1/render.py", "additions": 40, "deletions": 10}]),
            commit("b" * 40, "2026-11-15T11:00:00Z", "feat: squash-merged PR #9", [
                {"filename": "services/export/pdf_v1/api.py", "additions": 500, "deletions": 0}]),
            commit("c" * 40, "2026-11-15T12:00:00Z", "Merge branch 'x'", [], parents=2),
        ]
        self.calls: list[str] = []

    async def updated_prs(self, repo, since):
        return
        yield

    async def default_branch(self, repo):
        return "main"

    async def branch_commits(self, repo, branch, since):
        for c in self.commits:
            if not since or c["commit"]["committer"]["date"] > since:
                yield {k: c[k] for k in ("sha", "parents", "commit")}

    async def commit_has_pull_request(self, repo, sha):
        return sha.startswith("b")

    async def commit_detail(self, repo, sha):
        self.calls.append(sha)
        return next(c for c in self.commits if c["sha"] == sha)


def test_direct_commits_are_counted_once_and_prs_are_not_double_counted():
    async def go():
        org = await resolve_org(config.default_org_slug)
        gh = FakeGitHub()
        try:
            async with org_tx(org) as conn:
                integ = await conn.fetchval("SELECT id FROM integrations WHERE kind = 'github' LIMIT 1")
                manifest = await active_manifest(conn)
                stats = await sync_repository(conn, org, str(integ), REPO, gh, manifest)
                assert stats["direct_commits"] == 1 and stats["commits_in_prs"] == 1, stats
                assert gh.calls == ["a" * 40]                         # the merge commit is not even fetched
                rows = await conn.fetch("""SELECT w.external_id, w.item_type::text, w.kind::text, f.key
                                             FROM work_items w JOIN work_item_allocations a ON a.work_item_id = w.id
                                             JOIN features f ON f.id = a.feature_id
                                            WHERE w.external_id LIKE $1""", REPO + "@%")
                assert [(r["external_id"], r["item_type"], r["kind"], r["key"]) for r in rows] == [
                    (f"{REPO}@{'a' * 12}", "commit", "bug", "legacy-pdf-export")]
                again = await sync_repository(conn, org, str(integ), REPO, gh, manifest)   # cursor: nothing new
                assert again["direct_commits"] == 0 and gh.calls == ["a" * 40]

                # ...and the P&L actually counts it (regression: the first real run stored commits that every
                # reader filtered out with item_type = 'pr', so engineering cost stayed $0)
                from datetime import date
                from app.jobs.compute_pnl import load_dataset
                ds = await load_dataset(conn, [date(2026, 11, 1)])
                mine = [w for w in ds.work_items if w.id in {str(r["id"]) for r in await conn.fetch(
                    "SELECT id FROM work_items WHERE external_id LIKE $1", REPO + "@%")}]
                assert len(mine) == 1 and mine[0].allocations
        finally:
            async with org_tx(org) as conn:
                await conn.execute("DELETE FROM work_items WHERE external_id LIKE $1", REPO + "@%")
                await conn.execute("DELETE FROM ingest_inbox WHERE external_id LIKE $1", REPO + "@%")
                await conn.execute("DELETE FROM sync_cursors WHERE entity_type LIKE $1", "%" + REPO)
                await conn.execute("DELETE FROM repositories WHERE full_name = $1", REPO)
            await close_pool()
    asyncio.run(go())
