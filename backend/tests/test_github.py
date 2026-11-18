import json
import random

import pytest

from app.core.manifest import parse_manifest
from app.core.types import WorkKind
from app.ingest.github import (backoff_seconds, classify_kind, idempotency_key, normalize_pr,
                               parse_next_link)

MANIFEST = parse_manifest("""
version: 1
features:
  - {key: legacy-pdf-export, name: Legacy PDF, owner_team: docs-platform, paths: ["services/export/pdf_v1/**"]}
  - {key: editor, name: Editor, owner_team: editor, paths: ["web/src/editor/**"]}
""")

RAW_PR = {
    "number": 812, "title": "fix: PDF margin overflow (DOC-4411)", "state": "closed",
    "created_at": "2026-10-02T09:00:00Z", "updated_at": "2026-10-03T10:00:00Z", "merged_at": "2026-10-03T09:30:00Z",
    "body": "Fixes DOC-4411. Thanks @alice for the repro!",
    "user": {"login": "alice", "id": 1}, "assignees": [{"login": "bob"}], "requested_reviewers": [{"login": "carol"}],
    "labels": [{"name": "bug"}, {"name": "P1"}],
    "additions": 999, "deletions": 999, "changed_files": 3,
}
FILES = [
    {"filename": "services/export/pdf_v1/layout.py", "additions": 90, "deletions": 20, "sha": "x", "patch": "@@ secret code"},
    {"filename": "web/src/editor/toolbar.tsx", "additions": 10, "deletions": 0, "patch": "+code"},
    {"filename": "package-lock.json", "additions": 4000, "deletions": 3000},
]
REVIEWS = [{"user": {"login": "carol"}, "state": "CHANGES_REQUESTED", "body": "nit"},
           {"user": {"login": "carol"}, "state": "APPROVED"}]


def test_normalize_pr_scrubs_identity_and_code():
    n = normalize_pr("acme/web", RAW_PR, FILES, REVIEWS, MANIFEST)
    blob = json.dumps(n.__dict__, default=str)
    for leaked in ("alice", "bob", "carol", "secret code", "Thanks"):
        assert leaked not in blob
    assert n.external_id == "acme/web#812"
    assert n.kind is WorkKind.BUG and n.priority == "P1"
    assert n.linked_keys == ["DOC-4411"]
    assert (n.additions, n.deletions, n.files_changed) == (100, 20, 2)  # lockfile excluded
    assert n.review_rounds == 1
    assert n.allocations == {"legacy-pdf-export": pytest.approx(100 / 110, abs=1e-5),
                             "editor": pytest.approx(10 / 110, abs=1e-5)}
    assert n.owner_team_key == "docs-platform"
    assert not n.is_sweeping


def test_classify_kind():
    assert classify_kind([], 'Revert "add thing"')[0] is WorkKind.REVERT
    assert classify_kind(["incident", "P0"], "stop the bleeding") == (WorkKind.INCIDENT, "P0")
    assert classify_kind([], "hotfix: auth loop")[0] is WorkKind.HOTFIX
    assert classify_kind(["dependencies"], "bump react")[0] is WorkKind.CHORE
    assert classify_kind([], "Add approval chains")[0] is WorkKind.FEATURE


def test_idempotency_key_changes_with_version_only():
    a = idempotency_key("i", "pull_request", "acme/web#1", "2026-10-01T00:00:00Z")
    assert a == idempotency_key("i", "pull_request", "acme/web#1", "2026-10-01T00:00:00Z")
    assert a != idempotency_key("i", "pull_request", "acme/web#1", "2026-10-02T00:00:00Z")


def test_backoff_prefers_server_hints():
    assert backoff_seconds(0, retry_after="7") == 7
    assert backoff_seconds(0, reset_epoch="1000", now=990) == 11
    rng = random.Random(1)
    vals = [backoff_seconds(a, rng=rng) for a in range(12)]
    assert all(0 <= v <= 600 for v in vals)


def test_parse_next_link():
    h = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'
    assert parse_next_link(h) == "https://api.github.com/x?page=2"
    assert parse_next_link('<https://api.github.com/x?page=9>; rel="last"') is None


def test_pr_title_is_used_for_kind_but_never_stored():
    pr = dict(RAW_PR, labels=[], title="hotfix: auth loop for Jane Doe")
    n = normalize_pr("acme/web", pr, FILES, REVIEWS, MANIFEST)
    assert n.kind is WorkKind.HOTFIX
    assert "Jane" not in json.dumps(n.__dict__, default=str) and "auth loop" not in json.dumps(n.__dict__, default=str)
