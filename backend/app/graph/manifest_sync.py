"""Automatic features.yaml sync on GitHub push.

GitHub sends a `push` event; if any commit on the default branch added or modified `features.yaml` or
`.featureledger/features.yaml`, the file is fetched at the pushed commit, validated, and applied through
`manifest_store.record_and_apply`. An invalid file is recorded (with its structured errors) on the
manifest history and the GitHub integration's health; the active manifest and every mapping stay as they were.
A removed manifest is recorded as `removed` and changes nothing: deleting ownership must be deliberate.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .manifest_store import ApplyResult, record_and_apply

MANIFEST_PATHS = ("features.yaml", ".featureledger/features.yaml")


class Fetcher(Protocol):
    async def fetch(self, repo: str, path: str, sha: str) -> str: ...


@dataclass
class PushPlan:
    relevant: bool
    reason: str
    repo: str | None = None
    sha: str | None = None
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def plan_push(payload: dict[str, Any]) -> PushPlan:
    repo = (payload.get("repository") or {}).get("full_name")
    default = (payload.get("repository") or {}).get("default_branch") or "main"
    ref = payload.get("ref", "")
    if ref != f"refs/heads/{default}":
        return PushPlan(False, f"ignored: {ref or 'no ref'} is not the default branch ({default})", repo)
    if payload.get("deleted"):
        return PushPlan(False, "ignored: branch deletion", repo)
    changed, removed = set(), set()
    for c in (payload.get("commits") or []) + ([payload["head_commit"]] if payload.get("head_commit") else []):
        for p in (c.get("added") or []) + (c.get("modified") or []):
            if p in MANIFEST_PATHS:
                changed.add(p)
        for p in c.get("removed") or []:
            if p in MANIFEST_PATHS:
                removed.add(p)
    removed -= changed
    if not changed and not removed:
        return PushPlan(False, "ignored: no features.yaml change", repo, payload.get("after"))
    # .featureledger/features.yaml takes precedence when both exist
    ordered = sorted(changed, key=lambda p: 0 if p.startswith(".featureledger/") else 1)
    return PushPlan(True, "manifest changed", repo, payload.get("after"), ordered, sorted(removed))


class GitHubContentsFetcher:
    def __init__(self, token: str):
        self.token = token

    async def fetch(self, repo: str, path: str, sha: str) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.token}",
                                                          "Accept": "application/vnd.github+json",
                                                          "X-GitHub-Api-Version": "2022-11-28"}) as c:
            r = await c.get(f"https://api.github.com/repos/{repo}/contents/{path}", params={"ref": sha})
            r.raise_for_status()
            body = r.json()
            return base64.b64decode(body["content"]).decode("utf-8")


class PayloadFetcher:
    """SANDBOX_MODE only: the push payload carries the file content under `featureledger_sandbox_files`,
    standing in for the repository, so the full webhook path can be exercised without GitHub."""

    def __init__(self, payload: dict[str, Any]):
        self.files = payload.get("featureledger_sandbox_files") or {}

    async def fetch(self, repo: str, path: str, sha: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def default_fetcher(payload: dict[str, Any], sandbox: bool) -> Fetcher:
    if sandbox:
        return PayloadFetcher(payload)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to fetch features.yaml from GitHub")
    return GitHubContentsFetcher(token)


@dataclass
class PushResult:
    plan: PushPlan
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return any(r["status"] == "applied" for r in self.results)


async def handle_push(conn, org_id: str, payload: dict[str, Any], fetcher: Fetcher,
                      apply: Callable[..., Awaitable[ApplyResult]] = record_and_apply) -> PushResult:
    plan = plan_push(payload)
    out = PushResult(plan)
    if not plan.relevant:
        return out
    for path in plan.removed:
        await conn.execute("""INSERT INTO manifest_versions (org_id, source, repo, path, commit_sha, status)
                              VALUES ($1, 'github_push', $2, $3, $4, 'removed')""", org_id, plan.repo, path, plan.sha)
        out.results.append({"path": path, "status": "removed", "errors": []})
    if plan.changed:
        path = plan.changed[0]
        try:
            text = await fetcher.fetch(plan.repo, path, plan.sha)
        except Exception as e:  # noqa: BLE001 - fetch failure is recorded like an invalid manifest
            res = ApplyResult("invalid", [f"could not fetch {path} at {plan.sha}: {e}"])
            await conn.execute("""INSERT INTO manifest_versions (org_id, source, repo, path, commit_sha, status, errors)
                                  VALUES ($1, 'github_push', $2, $3, $4, 'invalid', $5)""",
                               org_id, plan.repo, path, plan.sha, res.errors)
        else:
            res = await apply(conn, org_id, text, source="github_push", repo=plan.repo, path=path, commit_sha=plan.sha)
        out.results.append({"path": path, "status": res.status, "errors": res.errors, "summary": res.summary})
    return out
