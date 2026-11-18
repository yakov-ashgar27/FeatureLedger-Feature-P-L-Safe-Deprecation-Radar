"""Open the cleanup PR: live GitHub REST API (Git Data API, one commit) or the sandbox provider.

Live sequence (GITHUB_CLEANUP_TOKEN, fine-grained: Contents RW + Pull requests RW on SUNSET_CLEANUP_REPO
only; the ingest token stays read-only):

    GET  /repos/{repo}                              default branch (unless SUNSET_CLEANUP_BASE)
    GET  /repos/{repo}/git/ref/heads/{base}         base commit
    GET  /repos/{repo}/git/commits/{sha}            base tree
    GET  /repos/{repo}/git/trees/{tree}?recursive=1 files that actually exist (deletions are intersected)
    GET  /repos/{repo}/contents/features.yaml       manifest as committed on the base branch
    POST /repos/{repo}/git/trees                    deletions (sha: null) + manifest edit + cleanup patch
    POST /repos/{repo}/git/commits                  one commit, parent = base
    POST /repos/{repo}/git/refs                     sunset/<feature_key>  (exists -> PATCH force)
    POST /repos/{repo}/pulls                        the PR (exists -> reuse the open one, refresh body)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ..config import config
from ..connectors.base import Transport
from .change import CleanupChange


class CleanupProviderError(RuntimeError):
    pass


@dataclass
class CleanupPR:
    provider: str
    repo: str
    number: int
    url: str | None
    branch: str
    base_branch: str
    head_sha: str | None
    files_deleted: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CleanupPRProvider(Protocol):
    name: str
    repo: str

    async def list_files(self) -> list[str] | None: ...
    async def manifest_text(self, path: str) -> str | None: ...
    async def open_pr(self, change: CleanupChange, *, next_number: int) -> CleanupPR: ...
    async def close_pr(self, number: int, comment: str) -> None: ...


def _ok(r, what: str, allow: tuple[int, ...] = ()) -> Any:
    if r.status >= 400 and r.status not in allow:
        msg = r.body.get("message") if isinstance(r.body, dict) else str(r.body)[:200]
        raise CleanupProviderError(f"GitHub {what} failed: HTTP {r.status} {msg}")
    return r.body


class GitHubCleanupProvider:
    name = "github"

    def __init__(self, transport: Transport, repo: str, base: str | None = None):
        self.t, self.repo, self.base = transport, repo, base
        self._base_sha: str | None = None
        self._tree_sha: str | None = None
        self._files: list[str] | None = None

    async def _resolve_base(self) -> tuple[str, str, str]:
        if not self.base:
            self.base = _ok(await self.t.request("GET", f"/repos/{self.repo}"), "read repo")["default_branch"]
        if not self._base_sha:
            ref = _ok(await self.t.request("GET", f"/repos/{self.repo}/git/ref/heads/{self.base}"), "read base ref")
            self._base_sha = ref["object"]["sha"]
            commit = _ok(await self.t.request("GET", f"/repos/{self.repo}/git/commits/{self._base_sha}"), "read commit")
            self._tree_sha = commit["tree"]["sha"]
        return self.base, self._base_sha, self._tree_sha  # type: ignore[return-value]

    async def list_files(self) -> list[str] | None:
        _, _, tree = await self._resolve_base()
        body = _ok(await self.t.request("GET", f"/repos/{self.repo}/git/trees/{tree}", params={"recursive": "1"}),
                   "list tree")
        if body.get("truncated"):
            return None
        self._files = [e["path"] for e in body.get("tree", []) if e.get("type") == "blob"]
        return self._files

    async def manifest_text(self, path: str) -> str | None:
        base, _, _ = await self._resolve_base()
        r = await self.t.request("GET", f"/repos/{self.repo}/contents/{path}", params={"ref": base})
        if r.status == 404:
            return None
        body = _ok(r, f"read {path}")
        return base64.b64decode(body.get("content", "")).decode()

    async def open_pr(self, change: CleanupChange, *, next_number: int = 0) -> CleanupPR:
        base, base_sha, tree_sha = await self._resolve_base()
        deletions = [p for p in change.deletions if self._files is None or p in set(self._files)]
        entries: list[dict[str, Any]] = [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in deletions]
        if change.manifest_after is not None:
            entries.append({"path": change.manifest_path, "mode": "100644", "type": "blob",
                            "content": change.manifest_after})
        entries.append({"path": change.patch_path, "mode": "100644", "type": "blob",
                        "content": json.dumps(change.patch, indent=2, default=str) + "\n"})
        tree = _ok(await self.t.request("POST", f"/repos/{self.repo}/git/trees",
                                        json_body={"base_tree": tree_sha, "tree": entries}), "create tree")
        commit = _ok(await self.t.request("POST", f"/repos/{self.repo}/git/commits", json_body={
            "message": f"{change.title}\n\nAutomated by FeatureLedger Safe Sunset (plan {change.patch['plan_id']}).",
            "tree": tree["sha"], "parents": [base_sha]}), "create commit")
        r = await self.t.request("POST", f"/repos/{self.repo}/git/refs",
                                 json_body={"ref": f"refs/heads/{change.branch}", "sha": commit["sha"]})
        if r.status == 422:   # branch left over from an earlier attempt: move it to the new commit
            _ok(await self.t.request("PATCH", f"/repos/{self.repo}/git/refs/heads/{change.branch}",
                                     json_body={"sha": commit["sha"], "force": True}), "update branch")
        else:
            _ok(r, "create branch")
        r = await self.t.request("POST", f"/repos/{self.repo}/pulls", json_body={
            "title": change.title, "head": change.branch, "base": base, "body": change.body,
            "maintainer_can_modify": True})
        if r.status == 422:
            owner = self.repo.split("/")[0]
            existing = _ok(await self.t.request("GET", f"/repos/{self.repo}/pulls",
                                                params={"head": f"{owner}:{change.branch}", "state": "open"}),
                           "find existing PR")
            if not existing:
                _ok(r, "open PR")
            pr = existing[0]
            _ok(await self.t.request("PATCH", f"/repos/{self.repo}/pulls/{pr['number']}",
                                     json_body={"title": change.title, "body": change.body}), "refresh PR")
        else:
            pr = _ok(r, "open PR")
        await self.t.request("POST", f"/repos/{self.repo}/issues/{pr['number']}/labels",
                             json_body={"labels": ["featureledger-sunset"]})   # best effort
        return CleanupPR("github", self.repo, int(pr["number"]), pr.get("html_url"), change.branch, base,
                         commit["sha"], deletions, change.files_edited + [change.patch_path])

    async def close_pr(self, number: int, comment: str) -> None:
        await self.t.request("POST", f"/repos/{self.repo}/issues/{number}/comments", json_body={"body": comment})
        _ok(await self.t.request("PATCH", f"/repos/{self.repo}/pulls/{number}", json_body={"state": "closed"}),
            "close PR")


class SandboxGitHubProvider:
    """Records the PR it would open (same CleanupChange as live) without a network call. A PR merge is
    simulated by POSTing a GitHub-shaped `pull_request` webhook to the real webhook endpoint."""
    name = "sandbox"

    def __init__(self, repo: str = "northwind/app", base: str = "main"):
        self.repo, self.base = repo, base
        self.opened: list[CleanupChange] = []
        self.closed: list[int] = []

    async def list_files(self) -> list[str] | None:
        return None   # use the files FeatureLedger has seen in merged PRs and import edges

    async def manifest_text(self, path: str) -> str | None:
        return None   # use the active manifest version

    async def open_pr(self, change: CleanupChange, *, next_number: int = 1001) -> CleanupPR:
        self.opened.append(change)
        sha = hashlib.sha1((change.body + change.branch).encode()).hexdigest()
        return CleanupPR("sandbox", self.repo, next_number, None, change.branch, self.base, sha,
                         list(change.deletions), change.files_edited + [change.patch_path])

    async def close_pr(self, number: int, comment: str) -> None:
        self.closed.append(number)


_override: CleanupPRProvider | None = None


def set_cleanup_provider(p: CleanupPRProvider | None) -> None:
    global _override
    _override = p


def get_cleanup_provider() -> CleanupPRProvider:
    if _override is not None:
        return _override
    token = os.environ.get("GITHUB_CLEANUP_TOKEN")
    if token and config.cleanup_repo:
        from ..connectors.http import HttpxTransport
        t = HttpxTransport("https://api.github.com", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "featureledger-sunset"})
        return GitHubCleanupProvider(t, config.cleanup_repo, config.cleanup_base_branch)
    if config.sandbox_mode:
        return SandboxGitHubProvider(config.cleanup_repo or "northwind/app", config.cleanup_base_branch or "main")
    raise CleanupProviderError("set SUNSET_CLEANUP_REPO and GITHUB_CLEANUP_TOKEN (Contents + Pull requests: "
                               "read & write on that repo) to open cleanup PRs")
