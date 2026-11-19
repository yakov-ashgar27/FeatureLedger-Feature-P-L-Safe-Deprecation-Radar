"""Where the Zombie-Guard comment goes (GitHub issue comments, or nowhere in sandbox) and where the
PR's changed files come from (GitHub's PR files API, or the webhook payload in sandbox)."""
from __future__ import annotations

import itertools
import os
from typing import Any, Protocol

from ..config import config
from ..connectors.base import Transport

MARKER = "<!-- featureledger:zombie-guard -->"


class CommentProviderError(RuntimeError):
    pass


class CommentProvider(Protocol):
    name: str

    async def find(self, repo: str, number: int) -> int | None: ...
    async def create(self, repo: str, number: int, body: str) -> int: ...
    async def update(self, repo: str, comment_id: int, body: str) -> None: ...


def _ok(r, what: str) -> Any:
    if r.status >= 400:
        msg = r.body.get("message") if isinstance(r.body, dict) else str(r.body)[:200]
        raise CommentProviderError(f"GitHub {what} failed: HTTP {r.status} {msg}")
    return r.body


class GitHubCommentProvider:
    """Issue comments on the PR. Needs Pull requests (or Issues): write on the repo."""
    name = "github"

    def __init__(self, transport: Transport):
        self.t = transport

    async def find(self, repo: str, number: int) -> int | None:
        for page in range(1, 11):     # our marker survives a FeatureLedger database reset
            body = _ok(await self.t.request("GET", f"/repos/{repo}/issues/{number}/comments",
                                            params={"per_page": 100, "page": page}), "list comments")
            for c in body or []:
                if MARKER in (c.get("body") or ""):
                    return int(c["id"])
            if not body or len(body) < 100:
                return None
        return None

    async def create(self, repo: str, number: int, body: str) -> int:
        return int(_ok(await self.t.request("POST", f"/repos/{repo}/issues/{number}/comments",
                                            json_body={"body": body}), "create comment")["id"])

    async def update(self, repo: str, comment_id: int, body: str) -> None:
        _ok(await self.t.request("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json_body={"body": body}),
            "update comment")


class SandboxCommentProvider:
    """No network: the comment lives in pr_guard_comments (visible in the dashboard)."""
    name = "sandbox"
    _ids = itertools.count(9_000_001)

    def __init__(self) -> None:
        self.created: list[tuple[str, int, str]] = []
        self.updated: list[tuple[int, str]] = []

    async def find(self, repo: str, number: int) -> int | None:
        return None

    async def create(self, repo: str, number: int, body: str) -> int:
        self.created.append((repo, number, body))
        return next(self._ids)

    async def update(self, repo: str, comment_id: int, body: str) -> None:
        self.updated.append((comment_id, body))


class FilesError(RuntimeError):
    pass


async def pr_files(payload: dict[str, Any], transport: Transport | None = None) -> list[str]:
    """Changed paths of the PR. Sandbox payloads carry them inline (`featureledger_sandbox_files`)."""
    if isinstance(payload.get("featureledger_sandbox_files"), list):
        return [str(p) for p in payload["featureledger_sandbox_files"]]
    repo = (payload.get("repository") or {}).get("full_name")
    number = payload.get("number") or (payload.get("pull_request") or {}).get("number")
    if transport is None:
        if not config.github_token:
            raise FilesError("set GITHUB_TOKEN (read-only Pull requests) so Zombie-Guard can list a PR's files")
        from ..connectors.http import HttpxTransport
        transport = HttpxTransport("https://api.github.com", headers={
            "Authorization": f"Bearer {config.github_token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "featureledger-zombie-guard"})
    out: list[str] = []
    for page in range(1, 31):         # GitHub lists at most 3000 files per PR
        r = await transport.request("GET", f"/repos/{repo}/pulls/{number}/files", params={"per_page": 100, "page": page})
        if r.status >= 400:
            raise FilesError(f"GitHub PR files failed: HTTP {r.status}")
        for f in r.body or []:
            out.append(f["filename"])
            if f.get("previous_filename"):
                out.append(f["previous_filename"])
        if not r.body or len(r.body) < 100:
            break
    return out


_override: CommentProvider | None = None


def set_comment_provider(p: CommentProvider | None) -> None:
    global _override
    _override = p


def get_comment_provider(demo: bool = False) -> CommentProvider:
    if _override is not None:
        return _override
    token = os.environ.get("GITHUB_COMMENT_TOKEN") or os.environ.get("GITHUB_CLEANUP_TOKEN")
    if demo or not token:
        return SandboxCommentProvider()
    from ..connectors.http import HttpxTransport
    return GitHubCommentProvider(HttpxTransport("https://api.github.com", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "featureledger-zombie-guard"}))
