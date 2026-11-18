"""PostHog (Query API + Groups API), B2B usage at the tenant (group) level.

Streams
  `groups` : GET /api/projects/{id}/groups/?group_type_index=N  (paged by `next`), full refresh each run.
  `usage`  : POST /api/projects/{id}/query with two HogQL queries per calendar month:
               per (group, event): distinct active days, distinct persons, event count
               per group:          distinct active days across all events (the concentration denominator)
             Backfill: the last `backfill_months` complete months + the current one. Incremental: the
             previous month (late events) + the current one. Cursor = last month fully read.
Only aggregates leave PostHog: no person ids, no properties other than the group's name and domain.
"""
from __future__ import annotations

from datetime import date
from typing import Any, AsyncIterator

from .base import Batch, Connector, SyncCtx, add_months, content_hash, month_start
from .scrub import scrub_posthog_group

USAGE_HOGQL = """
SELECT {group_col} AS group_key, event,
       count(DISTINCT toDate(timestamp)) AS active_days,
       count(DISTINCT person_id) AS active_users,
       count() AS events
FROM events
WHERE timestamp >= {start} AND timestamp < {end} AND {group_col} != ''
GROUP BY group_key, event
ORDER BY group_key, event
LIMIT 500000
"""
ACTIVITY_HOGQL = """
SELECT {group_col} AS group_key, count(DISTINCT toDate(timestamp)) AS active_days
FROM events
WHERE timestamp >= {start} AND timestamp < {end} AND {group_col} != ''
GROUP BY group_key
LIMIT 500000
"""


class PostHogConnector(Connector):
    kind = "posthog"
    streams = ("groups", "usage")

    def __init__(self, transport, project_id: str, group_type_index: int = 0):
        super().__init__(transport)
        self.project_id = project_id
        self.gti = group_type_index
        self.group_col = f"$group_{group_type_index}"

    # ---------------------------------------------------------------- groups
    async def _groups(self, ctx: SyncCtx) -> AsyncIterator[Batch]:
        url: str | None = f"/api/projects/{self.project_id}/groups/"
        params: dict[str, Any] | None = {"group_type_index": self.gti, "limit": 500}
        while url:
            r = await self.t.request("GET", url, params=params)
            envs = []
            for g in r.body.get("results", []):
                s = scrub_posthog_group(g)
                envs.append(ctx.envelope("posthog_group", s["key"], content_hash(s), s))
            url, params = r.body.get("next"), None
            yield Batch("groups", envs, ctx.now().isoformat() if not url else None)

    # ---------------------------------------------------------------- usage
    async def _query(self, sql: str, start: date, end: date) -> list[list[Any]]:
        body = {"query": {"kind": "HogQLQuery", "query": sql.replace("{group_col}", self.group_col),
                          "values": {"start": start.isoformat(), "end": end.isoformat()}}}
        r = await self.t.request("POST", f"/api/projects/{self.project_id}/query/", json_body=body)
        return r.body.get("results", [])

    async def _month(self, ctx: SyncCtx, m: date) -> Batch:
        end = add_months(m, 1)
        envs = []
        for gk, event, days, users, events in await self._query(USAGE_HOGQL, m, end):
            p = {"month": m.isoformat(), "group_key": str(gk), "event": event, "active_days": int(days),
                 "active_users": int(users), "events": int(events)}
            envs.append(ctx.envelope("posthog_usage", f"{m.isoformat()}|{gk}|{event}", content_hash(p), p))
        for gk, days in await self._query(ACTIVITY_HOGQL, m, end):
            p = {"month": m.isoformat(), "group_key": str(gk), "active_days": int(days)}
            envs.append(ctx.envelope("posthog_activity", f"{m.isoformat()}|{gk}", content_hash(p), p))
        return Batch("usage", envs, m.isoformat())

    def _months(self, start: date, now: date) -> list[date]:
        out, m = [], start
        while m <= month_start(now):
            out.append(m)
            m = add_months(m, 1)
        return out

    async def backfill(self, ctx: SyncCtx, stream: str) -> AsyncIterator[Batch]:
        if stream == "groups":
            async for b in self._groups(ctx):
                yield b
            return
        now = month_start(ctx.now())
        for m in self._months(add_months(now, -ctx.backfill_months), now):
            yield await self._month(ctx, m)

    async def incremental(self, ctx: SyncCtx, stream: str, cursor: str) -> AsyncIterator[Batch]:
        if stream == "groups":
            async for b in self._groups(ctx):
                yield b
            return
        now = month_start(ctx.now())
        start = min(add_months(now, -1), date.fromisoformat(cursor[:10]))
        for m in self._months(start, now):
            yield await self._month(ctx, m)
