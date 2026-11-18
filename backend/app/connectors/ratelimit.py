"""Rate limiting and retries shared by every connector.

* Token bucket per (integration, API host), stored in Redis (atomic Lua) in the worker, or in memory
  for tests and single-process runs. Callers wait the returned number of seconds and try again.
* `RetryingTransport` wraps any transport: it takes a token before every request, honours
  `Retry-After` / `X-RateLimit-Reset` on 429 and 503, retries 5xx with exponential backoff and full
  jitter, and gives up after `max_attempts` (the runner then marks the sync run failed).
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlparse

from .base import HttpError, HttpResponse, Transport

# requests per second and burst, per source (conservative versus each vendor's published limits)
LIMITS: dict[str, tuple[float, int]] = {
    "jira": (5.0, 10),
    "posthog": (1.0, 5),       # the query API is the expensive one
    "stripe": (20.0, 40),
    "zendesk": (10.0, 20),
    "aws_cost_explorer": (1.0, 2),  # each Cost Explorer call is billed
    "github": (10.0, 20),
}
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def backoff_seconds(attempt: int, retry_after: str | None = None, reset_epoch: str | None = None,
                    now: float | None = None, base: float = 1.0, cap: float = 600.0,
                    rng: random.Random | None = None) -> float:
    """Server hints win (Retry-After seconds, then X-RateLimit-Reset epoch); otherwise full jitter."""
    now = time.time() if now is None else now
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), cap)
        except ValueError:
            pass
    if reset_epoch and reset_epoch.isdigit():
        return min(max(float(reset_epoch) - now, 0.0) + 1.0, cap)
    return (rng or random).uniform(0, min(cap, base * (2 ** attempt)))


class BucketStore(Protocol):
    async def take(self, key: str, rate: float, capacity: int, cost: float = 1.0) -> float:
        """Try to take `cost` tokens. Returns 0 on success, else seconds to wait before retrying."""


@dataclass
class MemoryBucketStore:
    clock: Callable[[], float] = time.monotonic
    state: dict[str, tuple[float, float]] = field(default_factory=dict)

    async def take(self, key: str, rate: float, capacity: int, cost: float = 1.0) -> float:
        now = self.clock()
        tokens, ts = self.state.get(key, (float(capacity), now))
        tokens = min(float(capacity), tokens + (now - ts) * rate)
        if tokens >= cost:
            self.state[key] = (tokens - cost, now)
            return 0.0
        self.state[key] = (tokens, now)
        return (cost - tokens) / rate


TAKE_LUA = """
local cap = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local now = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or cap
local ts = tonumber(b[2]) or now
tokens = math.min(cap, tokens + math.max(now - ts, 0) * rate)
local wait = 0
if tokens >= cost then tokens = tokens - cost else wait = (cost - tokens) / rate end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 3600)
return tostring(wait)
"""


class RedisBucketStore:
    """Shared across worker processes, so N workers never exceed one vendor limit together."""

    def __init__(self, redis: Any):
        self.redis = redis

    async def take(self, key: str, rate: float, capacity: int, cost: float = 1.0) -> float:
        wait = await self.redis.eval(TAKE_LUA, 1, f"fl:bucket:{key}", capacity, rate, time.time(), cost)
        return float(wait.decode() if isinstance(wait, bytes) else wait)


class RetryingTransport:
    def __init__(self, inner: Transport, store: BucketStore, bucket_key: str, rate: float, capacity: int,
                 max_attempts: int = 6, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 rng: random.Random | None = None):
        self.inner, self.store, self.key = inner, store, bucket_key
        self.rate, self.capacity, self.max_attempts = rate, capacity, max_attempts
        self.sleep, self.rng = sleep, rng
        self.stats = {"requests": 0, "throttled_waits": 0, "retries": 0}

    async def _token(self, url: str) -> None:
        host = urlparse(url).netloc or "sandbox"
        for _ in range(1000):
            wait = await self.store.take(f"{self.key}:{host}", self.rate, self.capacity)
            if wait <= 0:
                return
            self.stats["throttled_waits"] += 1
            await self.sleep(wait)
        raise RuntimeError("rate limiter never yielded a token")

    async def request(self, method: str, url: str, *, params=None, json_body=None, headers=None) -> HttpResponse:
        last: HttpResponse | None = None
        for attempt in range(self.max_attempts):
            await self._token(url)
            self.stats["requests"] += 1
            try:
                resp = await self.inner.request(method, url, params=params, json_body=json_body, headers=headers)
            except (OSError, TimeoutError):  # network blip: retry with backoff
                if attempt == self.max_attempts - 1:
                    raise
                self.stats["retries"] += 1
                await self.sleep(backoff_seconds(attempt, rng=self.rng))
                continue
            if resp.status < 400:
                return resp
            if resp.status not in RETRYABLE:
                raise HttpError(resp.status, url, resp.body)
            last = resp
            if attempt < self.max_attempts - 1:
                self.stats["retries"] += 1
                h = {k.lower(): v for k, v in resp.headers.items()}
                await self.sleep(backoff_seconds(attempt, h.get("retry-after"), h.get("x-ratelimit-reset"),
                                                 rng=self.rng))
        raise HttpError(last.status if last else 0, url, last.body if last else None)


def wrap(kind: str, integration_id: str, inner: Transport, store: BucketStore, **kw) -> RetryingTransport:
    rate, cap = LIMITS.get(kind, (5.0, 10))
    return RetryingTransport(inner, store, f"{integration_id}", rate, cap, **kw)
