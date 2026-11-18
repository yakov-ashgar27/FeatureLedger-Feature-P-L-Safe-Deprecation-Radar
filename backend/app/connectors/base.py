"""The unified connector contract (spec §3.1).

A connector is a set of *streams* (Jira issues, Stripe invoices, PostHog monthly usage...). Each stream
is read either as a backfill (no cursor yet) or incrementally (from its stored cursor) and yields
`Batch`es. The runner commits a batch's envelopes to `ingest_inbox` and advances the stream's cursor in
the SAME transaction, so a crash can replay a batch but never skip one.

Connectors are pure with respect to storage: they only talk to a `Transport` (live HTTP or sandbox
fixtures) and emit scrubbed, idempotent `Envelope`s. Normalization into canonical tables happens later,
in `processors.py`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Callable, Protocol


class NotConfigured(RuntimeError):
    """Credentials or settings for a live connector are missing."""


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: Any = None):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: Any


class Transport(Protocol):
    async def request(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                      json_body: Any = None, headers: dict[str, str] | None = None) -> HttpResponse: ...


def content_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def idempotency_key(integration_id: str, entity_type: str, external_id: str, version: str) -> str:
    return hashlib.sha256(f"{integration_id}|{entity_type}|{external_id}|{version}".encode()).hexdigest()


@dataclass(frozen=True)
class Envelope:
    integration_id: str
    entity_type: str          # e.g. 'jira_issue', 'stripe_invoice', 'posthog_usage'
    external_id: str
    external_version: str     # updated_at / event id / content hash: drives idempotency
    payload: dict[str, Any]   # ALREADY scrubbed (allowlist) by the connector
    observed_at: datetime

    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.integration_id, self.entity_type, self.external_id, self.external_version)


@dataclass
class Batch:
    stream: str
    envelopes: list[Envelope]
    cursor: str | None        # the stream cursor to store once this batch is committed


@dataclass
class SyncCtx:
    org_id: str
    integration_id: str
    config: dict[str, Any] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    backfill_months: int = 6

    def now(self) -> datetime:
        return self.clock()

    def envelope(self, entity_type: str, external_id: str, version: str, payload: dict[str, Any]) -> Envelope:
        return Envelope(self.integration_id, entity_type, str(external_id), str(version), payload, self.now())


def month_start(d: date | datetime) -> date:
    return date(d.year, d.month, 1)


def add_months(m: date, n: int) -> date:
    y, mo = divmod(m.month - 1 + n, 12)
    return date(m.year + y, mo + 1, 1)


class Connector:
    """Base class. Subclasses set `kind` and `streams`, and implement `backfill` and `incremental`."""

    kind: str = ""
    streams: tuple[str, ...] = ()

    def __init__(self, transport: Transport):
        self.t = transport

    async def backfill(self, ctx: SyncCtx, stream: str) -> AsyncIterator[Batch]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def incremental(self, ctx: SyncCtx, stream: str, cursor: str) -> AsyncIterator[Batch]:
        raise NotImplementedError
        yield  # pragma: no cover

    def handle_webhook(self, ctx: SyncCtx, headers: dict[str, str], body: bytes) -> list[Envelope]:
        """Turn a verified webhook into envelopes. Default: none (the webhook only triggers a sync)."""
        return []

    async def sync(self, ctx: SyncCtx, cursors: dict[str, str]) -> AsyncIterator[Batch]:
        for stream in self.streams:
            cur = cursors.get(stream)
            it = self.incremental(ctx, stream, cur) if cur else self.backfill(ctx, stream)
            async for batch in it:
                yield batch
