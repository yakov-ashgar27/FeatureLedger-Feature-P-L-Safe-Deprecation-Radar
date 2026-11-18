"""Phase 4 cleanup PRs: manifest surgery, PR contents, and the exact GitHub REST sequence."""
import asyncio
from pathlib import Path

import pytest

from app.cleanup.change import CleanupError, build_cleanup_change, remove_feature_block
from app.cleanup.github import CleanupProviderError, GitHubCleanupProvider, SandboxGitHubProvider
from app.core.manifest import parse_manifest
from app.core.sunset_artifacts import blast_radius
from tests.fakes import ScriptedTransport

TEXT = (Path(__file__).resolve().parents[2] / "features.yaml").read_text()
MANIFEST = parse_manifest(TEXT)


@pytest.mark.parametrize("key", ["zapier-v1-webhooks", "legacy-pdf-export", "bulk-csv-import-v1", "document-editor"])
def test_remove_feature_block_keeps_everything_else(key):
    out = remove_feature_block(TEXT, key)
    after = parse_manifest(out)
    assert key not in after.by_key()
    assert set(after.by_key()) == set(MANIFEST.by_key()) - {key}
    for k, spec in after.by_key().items():
        assert spec == MANIFEST.by_key()[k]
    assert [t.key for t in after.teams] == [t.key for t in MANIFEST.teams]
    assert "# ----------------------------------------------------------------- integrations" in out
    assert "\n\n\n" not in out.replace(TEXT, "")


def test_remove_flow_style_item_and_missing_key():
    text = "version: 1\nfeatures:\n  - {key: aa, name: A, paths: [\"a/**\"]}\n  - {key: bb, name: B}\n"
    assert set(parse_manifest(remove_feature_block(text, "aa")).by_key()) == {"bb"}
    with pytest.raises(CleanupError, match="not found"):
        remove_feature_block(text, "zzz")


def _change(files):
    spec = MANIFEST.by_key()["legacy-pdf-export"]
    edges = [("web/src/features/editor/toolbar.tsx", "web/src/features/export-pdf-legacy/button.tsx"),
             ("services/editor/handlers/export.py", "services/export/pdf_v1/api.py")]
    br = blast_radius(spec, MANIFEST, edges)
    br["inbound_references"][0]["acknowledged"] = True
    audit = [{"from_state": None, "to_state": "candidate_for_sunset", "action": "transition", "created_at": "2026-11-01T10:00",
              "payload": {"actor_role": "pm_tech_lead"}, "hash": "aa" * 32},
             {"from_state": "canary_dark_launch", "to_state": "canary_dark_launch", "action": "canary_promote",
              "created_at": "2026-11-02T10:00", "payload": {"actor_role": "admin"}, "hash": "bb" * 32}]
    scream = {"fully_off_at": "2026-11-02T10:00:00+00:00", "quiet_days": 15, "required_days": 14,
              "flag_on_evaluations": 0, "flag_off_evaluations": 900, "tickets_since_off": 0}
    return build_cleanup_change(spec=spec, manifest=MANIFEST, manifest_text=TEXT, manifest_path="features.yaml",
                                repo_files=files, blast_radius=br, plan={"id": "plan-1", "reason": "superseded by v2",
                                                                         "rollback_count": 1},
                                audit=audit, scream=scream)


FILES = ["services/export/pdf_v1/api.py", "services/export/pdf_v1/render.py", "services/export/pdf_v1/tests/test_render.py",
         "web/src/features/export-pdf-legacy/button.tsx", "services/export/pdf_v2/api.py", "services/editor/handlers/export.py",
         "services/export/pdf_v1/fixtures/sample.pdf"]


def test_cleanup_change_contents():
    ch = _change(FILES)
    assert ch.branch == "sunset/legacy-pdf-export"
    assert ch.title == "chore(sunset): remove Legacy PDF Exporter (legacy-pdf-export)"
    assert "services/export/pdf_v2/api.py" not in ch.deletions          # the replacement survives
    assert "services/editor/handlers/export.py" not in ch.deletions      # call sites are edited by hand
    assert "services/export/pdf_v1/tests/test_render.py" in ch.deletions
    assert "legacy-pdf-export" not in parse_manifest(ch.manifest_after).by_key()
    assert ch.patch["routes_to_remove"] == ["/api/v1/export-pdf*"]
    assert ch.patch["flag"]["key"] == "legacy-pdf-export" and ch.patch["audit_chain_head"] == "bb" * 32
    assert {c["path"] for c in ch.patch["call_sites_to_edit"]} == {"services/editor/handlers/export.py",
                                                                   "web/src/features/editor/toolbar.tsx"}
    body = ch.body
    for needle in ("### Blast radius", "Inbound references: **2** (1 acknowledged", "### Scream test", "| 15 | 14 | 0 | 900 | 0 |",
                   "### Audit trail", "canary_dark_launch → canary_dark_launch | canary_promote | admin",
                   "Chain head: `" + "bb" * 32, "remove route `/api/v1/export-pdf*`", "rollbacks so far: 1",
                   "Merging this PR archives the feature"):
        assert needle in body, needle
    assert "@" not in body.split("<details>")[0]     # roles, never people, in a public PR


def _gh(extra=None):
    routes = {
        ("GET", "/repos/acme/app/git/ref/heads/main"): {"object": {"sha": "base-sha"}},
        ("GET", "/repos/acme/app/git/commits/base-sha"): {"tree": {"sha": "tree-sha"}},
        ("GET", "/repos/acme/app/git/trees/tree-sha"): {"truncated": False, "tree": [
            {"path": p, "type": "blob"} for p in FILES if not p.endswith("render.py")] + [{"path": "services", "type": "tree"}]},
        ("GET", "/repos/acme/app/contents/features.yaml"): {"content": __import__("base64").b64encode(TEXT.encode()).decode()},
        ("GET", "/repos/acme/app"): {"default_branch": "main"},
        ("POST", "/repos/acme/app/git/trees"): {"sha": "new-tree"},
        ("POST", "/repos/acme/app/git/commits"): {"sha": "new-commit"},
        ("POST", "/repos/acme/app/git/refs"): {"ref": "refs/heads/sunset/legacy-pdf-export"},
        ("POST", "/repos/acme/app/pulls"): (201, {"number": 77, "html_url": "https://github.com/acme/app/pull/77"}),
        ("POST", "/repos/acme/app/issues/"): {},
        ("PATCH", "/repos/acme/app/"): {},
    }
    routes.update(extra or {})
    t = ScriptedTransport(routes)
    return GitHubCleanupProvider(t, "acme/app"), t


def test_github_provider_opens_one_commit_branch_and_pr():
    gh, t = _gh()
    files = asyncio.run(gh.list_files())
    assert "services/export/pdf_v1/render.py" not in files
    assert asyncio.run(gh.manifest_text("features.yaml")) == TEXT
    pr = asyncio.run(gh.open_pr(_change(FILES)))
    assert (pr.number, pr.url, pr.branch, pr.base_branch, pr.head_sha) == (
        77, "https://github.com/acme/app/pull/77", "sunset/legacy-pdf-export", "main", "new-commit")
    tree = [c for c in t.calls if c["method"] == "POST" and c["url"].endswith("/git/trees")][0]["json"]
    assert tree["base_tree"] == "tree-sha"
    deleted = [e["path"] for e in tree["tree"] if e.get("sha", "x") is None]
    assert "services/export/pdf_v1/api.py" in deleted
    assert "services/export/pdf_v1/render.py" not in deleted      # not in the repo tree: never "deleted"
    edits = {e["path"]: e for e in tree["tree"] if "content" in e}
    assert "legacy-pdf-export" not in parse_manifest(edits["features.yaml"]["content"]).by_key()
    assert ".featureledger/sunset/legacy-pdf-export.json" in edits
    commit = [c for c in t.calls if c["url"].endswith("/git/commits")][0]["json"]
    assert commit["parents"] == ["base-sha"] and commit["tree"] == "new-tree"
    ref = [c for c in t.calls if c["url"].endswith("/git/refs")][0]["json"]
    assert ref == {"ref": "refs/heads/sunset/legacy-pdf-export", "sha": "new-commit"}
    pull = [c for c in t.calls if c["method"] == "POST" and c["url"].endswith("/pulls")][0]["json"]
    assert pull["head"] == "sunset/legacy-pdf-export" and pull["base"] == "main" and "### Blast radius" in pull["body"]


def test_github_provider_reuses_existing_branch_and_pr():
    gh, t = _gh({("POST", "/repos/acme/app/git/refs"): (422, {"message": "Reference already exists"}),
                 ("POST", "/repos/acme/app/pulls"): (422, {"message": "A pull request already exists"}),
                 ("GET", "/repos/acme/app/pulls"): (200, [{"number": 12, "html_url": "https://github.com/acme/app/pull/12"}])})
    pr = asyncio.run(gh.open_pr(_change(FILES)))
    assert pr.number == 12
    force = [c for c in t.calls if c["method"] == "PATCH" and "/git/refs/heads/sunset/" in c["url"]][0]
    assert force["json"] == {"sha": "new-commit", "force": True}
    assert any(c["method"] == "PATCH" and c["url"].endswith("/pulls/12") for c in t.calls)


def test_github_provider_errors_and_close():
    gh, _ = _gh({("POST", "/repos/acme/app/git/trees"): (403, {"message": "Resource not accessible by integration"})})
    with pytest.raises(CleanupProviderError, match="403"):
        asyncio.run(gh.open_pr(_change(FILES)))
    gh, t = _gh()
    asyncio.run(gh.close_pr(77, "rolled back"))
    assert t.calls[-1]["method"] == "PATCH" and t.calls[-1]["json"] == {"state": "closed"}


def test_sandbox_provider_mirrors_live_contents():
    sb = SandboxGitHubProvider("northwind/app")
    pr = asyncio.run(sb.open_pr(_change(FILES), next_number=1001))
    assert pr.number == 1001 and pr.url is None and pr.branch == "sunset/legacy-pdf-export"
    assert pr.files_deleted == sb.opened[0].deletions
