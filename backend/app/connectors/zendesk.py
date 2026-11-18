"""Zendesk Support (Tickets + Metrics).

Streams
  `organizations`: GET /api/v2/organizations?page[size]=100 (cursor pages), full refresh: domain names
                   drive identity stitching.
  `tickets`: incremental cursor export GET /api/v2/incremental/tickets/cursor.json?start_time=...&include=metric_sets,
             then `?cursor=<after_cursor>` until `end_of_stream`. The `after_cursor` is the stream cursor.

Handle time: Zendesk has no native handle time. If `time_field_id` (the Time Tracking app's "total time
spent (sec)" field) is configured it is used; otherwise handle time is estimated as replies × 12 minutes
and the ticket is flagged `handle_time_estimated`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator

from .base import Batch, Connector, SyncCtx, add_months, month_start
from .scrub import scrub_zendesk_org, scrub_zendesk_ticket

MINUTES_PER_REPLY = 12


def handle_minutes(ticket: dict[str, Any]) -> tuple[int, bool]:
    if ticket.get("time_spent_seconds") is not None:
        return max(0, round(ticket["time_spent_seconds"] / 60)), False
    replies = ticket.get("replies") or 1
    return int(replies) * MINUTES_PER_REPLY, True


class ZendeskConnector(Connector):
    kind = "zendesk"
    streams = ("organizations", "tickets")

    def __init__(self, transport, time_field_id: int | None = None):
        super().__init__(transport)
        self.time_field_id = time_field_id

    async def _orgs(self, ctx: SyncCtx) -> AsyncIterator[Batch]:
        params: dict[str, Any] | None = {"page[size]": 100}
        url: str | None = "/api/v2/organizations"
        while url:
            r = await self.t.request("GET", url, params=params)
            envs = [ctx.envelope("zendesk_org", str(o["id"]), o.get("updated_at") or "", scrub_zendesk_org(o))
                    for o in r.body.get("organizations", [])]
            meta = r.body.get("meta") or {}
            if meta.get("has_more"):
                params = {"page[size]": 100, "page[after]": meta.get("after_cursor")}
            else:
                url = None
            yield Batch("organizations", envs, ctx.now().isoformat() if url is None else None)

    async def _tickets(self, ctx: SyncCtx, params: dict[str, Any]) -> AsyncIterator[Batch]:
        while True:
            r = await self.t.request("GET", "/api/v2/incremental/tickets/cursor.json", params=params)
            metrics = {m["ticket_id"]: m for m in r.body.get("metric_sets", [])}
            envs = []
            for t in r.body.get("tickets", []):
                s = scrub_zendesk_ticket(t, metrics.get(t["id"]), self.time_field_id)
                envs.append(ctx.envelope("zendesk_ticket", str(s["id"]), s["updated_at"] or "", s))
            cursor = r.body.get("after_cursor")
            yield Batch("tickets", envs, cursor)
            if r.body.get("end_of_stream") or not cursor:
                return
            params = {"cursor": cursor, "include": "metric_sets"}

    async def backfill(self, ctx: SyncCtx, stream: str) -> AsyncIterator[Batch]:
        if stream == "organizations":
            async for b in self._orgs(ctx):
                yield b
            return
        start = add_months(month_start(ctx.now()), -ctx.backfill_months)
        ts = int(datetime(start.year, start.month, 1, tzinfo=timezone.utc).timestamp())
        async for b in self._tickets(ctx, {"start_time": ts, "include": "metric_sets"}):
            yield b

    async def incremental(self, ctx: SyncCtx, stream: str, cursor: str) -> AsyncIterator[Batch]:
        if stream == "organizations":
            async for b in self._orgs(ctx):
                yield b
            return
        async for b in self._tickets(ctx, {"cursor": cursor, "include": "metric_sets"}):
            yield b
