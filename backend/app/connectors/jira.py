"""Jira Cloud (REST API v3).

Stream `issues`: `POST /rest/api/3/search/jql` with `updated >= "<cursor>" ORDER BY updated ASC`, paged by
`nextPageToken`. JQL has minute precision, so the incremental cursor overlaps by one minute; the inbox's
idempotency key (issue key + `updated`) makes the overlap free.

What Jira adds to the P&L: the issue type and priority behind a PR's linked key (a PR titled "tweak export"
whose issue is a P1 Bug is a bug fix), and label/epic ownership for PRs that touch no mapped path.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from .base import Batch, Connector, SyncCtx
from .scrub import scrub_jira_issue

FIELDS = ["issuetype", "priority", "labels", "status", "created", "updated", "resolutiondate", "parent",
          "project", "components", "summary", "description"]   # summary/description: Tier 3, memory only
PAGE = 100
TYPE_TO_KIND = {"bug": "bug", "incident": "incident", "story": "feature", "task": "chore", "sub-task": "chore",
                "subtask": "chore", "epic": "feature", "improvement": "feature", "spike": "chore"}
PRIORITY_TO_P = {"highest": "P0", "blocker": "P0", "high": "P1", "critical": "P1", "medium": "P2", "low": "P3",
                 "lowest": "P4", "trivial": "P4"}


def kind_of(issue: dict[str, Any]) -> str:
    return TYPE_TO_KIND.get((issue.get("issuetype") or "").lower(), "feature")


def priority_of(issue: dict[str, Any]) -> str | None:
    return PRIORITY_TO_P.get((issue.get("priority") or "").lower())


def adf_text(node: Any, limit: int = 2000) -> str:
    """Plain text of an Atlassian Document Format body (Jira v3 descriptions)."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if sum(len(x) for x in out) > limit:
            return
        if isinstance(n, dict):
            if n.get("type") == "text" and isinstance(n.get("text"), str):
                out.append(n["text"])
            for c in n.get("content") or []:
                walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)
        elif isinstance(n, str):
            out.append(n)
    walk(node)
    return " ".join(out)[:limit]


def jql_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def parse_jira_ts(s: str) -> datetime:
    # Jira: 2026-10-03T09:12:44.123+0000
    s = s.replace("Z", "+00:00")
    if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


class JiraConnector(Connector):
    kind = "jira"
    streams = ("issues",)

    def __init__(self, transport, project_keys: list[str] | None = None, backfill_days: int = 365):
        super().__init__(transport)
        self.project_keys = project_keys or []
        self.backfill_days = backfill_days

    def _jql(self, since: datetime) -> str:
        proj = f'project in ({",".join(self.project_keys)}) AND ' if self.project_keys else ""
        return f'{proj}updated >= "{jql_time(since)}" ORDER BY updated ASC, key ASC'

    async def _pages(self, ctx: SyncCtx, since: datetime) -> AsyncIterator[Batch]:
        token: str | None = None
        newest = since
        while True:
            body: dict[str, Any] = {"jql": self._jql(since), "fields": FIELDS, "maxResults": PAGE}
            if token:
                body["nextPageToken"] = token
            r = await self.t.request("POST", "/rest/api/3/search/jql", json_body=body)
            envs = []
            for raw in r.body.get("issues", []):
                s = scrub_jira_issue(raw)
                f = raw.get("fields") or {}
                text = f"{f.get('summary') or ''}\n{adf_text(f.get('description'))}"
                envs.append(ctx.envelope("jira_issue", s["key"], s["updated"] or "", s, text))
                if s["updated"]:
                    newest = max(newest, parse_jira_ts(s["updated"]))
            token = r.body.get("nextPageToken")
            # the cursor only moves once a page is committed; overlap 1 minute (JQL precision)
            yield Batch("issues", envs, (newest - timedelta(minutes=1)).isoformat())
            if not token or r.body.get("isLast", not token):
                return

    async def backfill(self, ctx: SyncCtx, stream: str) -> AsyncIterator[Batch]:
        async for b in self._pages(ctx, ctx.now() - timedelta(days=self.backfill_days)):
            yield b

    async def incremental(self, ctx: SyncCtx, stream: str, cursor: str) -> AsyncIterator[Batch]:
        async for b in self._pages(ctx, datetime.fromisoformat(cursor)):
            yield b

    def handle_webhook(self, ctx: SyncCtx, headers: dict[str, str], body: bytes) -> list:
        evt = json.loads(body or b"{}")
        issue = evt.get("issue")
        if not issue or not issue.get("key"):
            return []
        s = scrub_jira_issue(issue)
        return [ctx.envelope("jira_issue", s["key"], s["updated"] or evt.get("timestamp", ""), s)]
