"""Phase 5: Zombie-Guard comment rendering, idempotency, cleanup-PR exclusion, GitHub comment calls."""
import asyncio

import pytest

from app.guard import zombie_guard as zg
from app.guard.providers import MARKER, GitHubCommentProvider, SandboxCommentProvider, pr_files
from app.guard.zombie_guard import FeatureImpact, body_sha, handle_pull_request, render
from tests.fakes import ScriptedTransport


def impact(key="legacy-pdf-export", **kw):
    base = dict(key=key, name=key.replace("-", " ").title(), status="active", classification="zombie", net_3m=-68_592.0,
                adoption=0.025, gross_margin_pct=0.4, margin_bleed=False, eng_hours_month=307.0, sunset_state=None,
                canary_phase=None, scream_day=None, scream_days=None, cleanup_branch=None, files=2)
    base.update(kw)
    return FeatureImpact(**base)


def test_render_shows_net_adoption_and_stage():
    body = render([impact(), impact("document-editor", classification="cash_cow", net_3m=90_000.0, files=1)],
                  head_sha="abc1234def", total_files=3, other_features=1)
    assert body.startswith(MARKER)
    assert "| Zombie | −$68,592 | 2.5% | Zombie, no sunset plan yet | 2 |" in body
    assert "Document Editor" not in body.split("**Before you merge**")[0]   # healthy features are only counted
    assert "Also touches 1 healthy feature(s)." in body and "`abc1234`" in body and "never blocks a merge" in body
    canary = impact(sunset_state="canary_dark_launch", canary_phase="fully_off", scream_day=12, scream_days=30)
    assert "flag fully off, scream test day 12/30" in render([canary], head_sha=None, total_files=1, other_features=0)
    pr = impact(sunset_state="dead_code_pr_opened", cleanup_branch="sunset/legacy-pdf-export")
    assert "`sunset/legacy-pdf-export` deletes this code" in render([pr], head_sha=None, total_files=1, other_features=0)
    assert "Archived" in render([impact(status="archived", classification=None)], head_sha=None, total_files=1,
                                other_features=0)


def test_body_hash_ignores_the_footer():
    a = render([impact()], head_sha="a" * 40, total_files=3, other_features=0)
    b = render([impact()], head_sha="b" * 40, total_files=4, other_features=0)
    assert a != b and body_sha(a) == body_sha(b)
    assert body_sha(a) != body_sha(render([impact(net_3m=-1.0)], head_sha=None, total_files=3, other_features=0))


class FakeConn:
    """Just enough of asyncpg for handle_pull_request: a cleanup-PR table and the comment table."""
    def __init__(self, cleanup=()):
        self.cleanup = set(cleanup)
        self.rows: dict[tuple, dict] = {}

    async def fetchval(self, sql, repo, number):
        assert "cleanup_pull_requests" in sql
        return 1 if (repo, number) in self.cleanup else None

    async def fetchrow(self, sql, repo, number):
        return self.rows.get((repo, number))

    async def execute(self, sql, *a):
        if sql.lstrip().startswith("INSERT"):
            org, repo, number, provider, cid, head, body, sha, feats, flagged = a
            self.rows[(repo, number)] = {"comment_id": cid, "body": body, "body_sha": sha, "head_sha": head,
                                         "updates": 0, "flagged": flagged, "features": feats}
        elif "body = $3" in sql:
            repo, number, body, sha, feats, flagged, head = a
            r = self.rows[(repo, number)]
            r.update(body=body, body_sha=sha, features=feats, flagged=flagged, head_sha=head, updates=r["updates"] + 1)
        else:
            repo, number, head = a
            self.rows[(repo, number)]["head_sha"] = head


def payload(action, sha, number=42, ref="fix/pdf-margins"):
    return {"action": action, "number": number, "repository": {"full_name": "acme/app"},
            "pull_request": {"number": number, "head": {"ref": ref, "sha": sha}}}


@pytest.fixture
def touched(monkeypatch):
    state = {"impacts": [impact()]}

    async def fake_assess(conn, paths):
        return list(state["impacts"]), 0
    monkeypatch.setattr(zg, "assess", fake_assess)
    return state


def test_one_comment_per_pr_updated_in_place(touched):
    conn, bot = FakeConn(), SandboxCommentProvider()
    run = lambda p: asyncio.run(handle_pull_request(conn, "org", p, bot, ["a.py", "b.py"]))  # noqa: E731
    assert run(payload("opened", "a" * 40))["zombie_guard"] == "created"
    assert run(payload("synchronize", "b" * 40))["zombie_guard"] == "unchanged"      # same facts: no edit
    touched["impacts"] = [impact(), impact("zapier-v1-webhooks", sunset_state="canary_dark_launch")]
    r = run(payload("synchronize", "c" * 40))
    assert r["zombie_guard"] == "updated" and r["features"] == ["legacy-pdf-export", "zapier-v1-webhooks"]
    touched["impacts"] = [impact("document-editor", classification="cash_cow")]
    r = run(payload("synchronize", "d" * 40))
    assert r["zombie_guard"] == "updated" and r["features"] == []
    assert "no longer touches" in conn.rows[("acme/app", 42)]["body"]
    assert len(bot.created) == 1 and len(bot.updated) == 2                          # never a second comment
    assert conn.rows[("acme/app", 42)]["updates"] == 2 and conn.rows[("acme/app", 42)]["head_sha"] == "d" * 40


def test_clean_prs_get_no_comment(touched):
    touched["impacts"] = [impact("document-editor", classification="cash_cow")]
    conn, bot = FakeConn(), SandboxCommentProvider()
    assert asyncio.run(handle_pull_request(conn, "org", payload("opened", "a" * 40), bot, ["x"]))["zombie_guard"] == "clean"
    assert not bot.created and not conn.rows


def test_featureledgers_own_cleanup_pr_is_ignored(touched):
    conn, bot = FakeConn(cleanup={("acme/app", 77)}), SandboxCommentProvider()
    by_branch = asyncio.run(handle_pull_request(conn, "org", payload("opened", "a" * 40, 50, "sunset/legacy-pdf-export"),
                                                bot, ["x"]))
    by_record = asyncio.run(handle_pull_request(conn, "org", payload("opened", "a" * 40, 77, "renamed-branch"), bot, ["x"]))
    assert by_branch["zombie_guard"] == by_record["zombie_guard"] == "ignored"
    assert not bot.created


def test_github_comment_provider_reuses_a_marked_comment():
    t = ScriptedTransport({("GET", "/repos/acme/app/issues/42/comments"): (200, [{"id": 5, "body": "LGTM"},
                                                                                 {"id": 6, "body": MARKER + "\nold"}]),
                           ("POST", "/repos/acme/app/issues/42/comments"): (201, {"id": 7}),
                           ("PATCH", "/repos/acme/app/issues/comments/"): {}})
    gh = GitHubCommentProvider(t)
    assert asyncio.run(gh.find("acme/app", 42)) == 6
    assert asyncio.run(gh.create("acme/app", 42, "hi")) == 7
    asyncio.run(gh.update("acme/app", 6, "new"))
    assert t.calls[-1]["url"] == "/repos/acme/app/issues/comments/6" and t.calls[-1]["json"] == {"body": "new"}


def test_pr_files_from_github_and_sandbox():
    assert asyncio.run(pr_files({"featureledger_sandbox_files": ["a.py"]})) == ["a.py"]
    t = ScriptedTransport({("GET", "/repos/acme/app/pulls/42/files"): (200, [
        {"filename": "services/export/pdf_v1/api.py"},
        {"filename": "services/export/pdf_v2/api.py", "previous_filename": "services/export/pdf_v1/old.py"}])})
    files = asyncio.run(pr_files({"repository": {"full_name": "acme/app"}, "number": 42}, t))
    assert files == ["services/export/pdf_v1/api.py", "services/export/pdf_v2/api.py", "services/export/pdf_v1/old.py"]
