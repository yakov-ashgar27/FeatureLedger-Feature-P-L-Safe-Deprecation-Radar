"""Rate limiting: fixed-window counters, shared through Redis, with an in-process fallback.

Rules (per minute unless noted):

    POST /api/v1/auth/dev-login           30 per client address   (password-less sandbox login)
    GET  /api/v1/auth/oidc/*              30 per client address   (SSO redirects / callbacks)
    POST /api/v1/demo/launch|switch-role  30 per client address   (anonymous demo sessions)
    POST /api/v1/webhooks/*             1200 per client address   (vendors retry in bursts)
    everything else under /api/v1       1200 per session token, or per address when anonymous
    POST /api/v1/demo/reset               once per DEMO_RESET_COOLDOWN_S for the whole demo (in the route)

Behind the dashboard every browser call reaches the API from the Next.js server, so the client address is
taken from X-Forwarded-For, TRUSTED_PROXY_HOPS entries from the right (1 = Next.js only, 2 = a load
balancer in front of it). Keep the API itself off the public internet, or that header can be forged.

If Redis is unreachable the limiter keeps counting in process memory (per worker) and retries Redis after
30 s: a Redis outage weakens the limits, it never blocks sign-in.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("featureledger.ratelimit")


@dataclass(frozen=True)
class Rule:
    name: str
    limit: int
    window_s: int
    by_session: bool = False


LOGIN = Rule("login", 30, 60)
SSO = Rule("sso", 30, 60)
DEMO_SESSION = Rule("demo-session", 30, 60)
WEBHOOK = Rule("webhook", 1200, 60)
API = Rule("api", 1200, 60, by_session=True)


def rule_for(method: str, path: str) -> Rule | None:
    if not path.startswith("/api/v1/"):
        return None                                   # /health, /docs
    if method == "POST" and path == "/api/v1/auth/dev-login":
        return LOGIN
    if path.startswith("/api/v1/auth/oidc/"):
        return SSO
    if method == "POST" and path in ("/api/v1/demo/launch", "/api/v1/demo/switch-role"):
        return DEMO_SESSION
    if method == "POST" and path.startswith("/api/v1/webhooks/"):
        return WEBHOOK
    return API


def client_address(peer: str | None, forwarded_for: str | None, trusted_hops: int) -> str:
    """The caller's address: the peer, or the entry `trusted_hops` from the right of X-Forwarded-For."""
    if trusted_hops > 0 and forwarded_for:
        hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
        if hops:
            return hops[-min(trusted_hops, len(hops))]
    return peer or "unknown"


def client_key(rule: Rule, address: str, bearer: str | None, cookie: str | None) -> str:
    token = bearer or cookie
    if rule.by_session and token:
        return "s:" + hashlib.sha256(token.encode()).hexdigest()[:24]
    return "a:" + address


class MemoryCounter:
    def __init__(self) -> None:
        self._c: dict[str, tuple[int, float]] = {}

    def hit(self, key: str, window_s: int, now: float) -> tuple[int, float]:
        start = now - (now % window_s)
        n, s = self._c.get(key, (0, start))
        n = n + 1 if s == start else 1
        self._c[key] = (n, start)
        if len(self._c) > 50_000:                     # drop finished windows
            self._c = {k: v for k, v in self._c.items() if v[1] + window_s > now}
        return n, start + window_s


class RateLimiter:
    def __init__(self, redis_url: str | None, prefix: str = "fl:rl:") -> None:
        self.redis_url, self.prefix = redis_url, prefix
        self.memory = MemoryCounter()
        self._redis: Any = None
        self._redis_down_until = 0.0
        self._cooldowns: dict[str, float] = {}

    async def _client(self) -> Any:
        if not self.redis_url or time.time() < self._redis_down_until:
            return None
        if self._redis is None:
            try:
                from redis.asyncio import Redis
            except ImportError:
                log.warning("rate limiter: redis package not installed; counting in process memory")
                self.redis_url = None
                return None
            self._redis = Redis.from_url(self.redis_url, socket_connect_timeout=0.25, socket_timeout=0.25)
        return self._redis

    async def hit(self, key: str, limit: int, window_s: int) -> tuple[bool, int]:
        """-> (allowed, seconds until the window resets)."""
        now = time.time()
        start = now - (now % window_s)
        reset = start + window_s
        r = await self._client()
        n = None
        if r is not None:
            k = f"{self.prefix}{key}:{int(start)}"
            try:
                async with r.pipeline(transaction=True) as p:
                    p.incr(k)
                    p.expire(k, window_s + 1)
                    n = (await p.execute())[0]
            except Exception as e:  # noqa: BLE001 - any Redis problem: fall back, retry later
                if self._redis_down_until < now:
                    log.warning("rate limiter: Redis unavailable (%s); counting in process memory", e)
                self._redis_down_until = now + 30
        if n is None:
            n, reset = self.memory.hit(key, window_s, now)
        return n <= limit, max(1, int(reset - now + 0.999))

    async def cooldown(self, key: str, seconds: int) -> tuple[bool, int]:
        """At most once per `seconds`, measured from the last allowed call. -> (allowed, seconds to wait)."""
        now = time.time()
        k = f"{self.prefix}cd:{key}"
        r = await self._client()
        if r is not None:
            try:
                if await r.set(k, "1", nx=True, ex=seconds):
                    return True, 0
                return False, max(1, int(await r.ttl(k) or seconds))
            except Exception as e:  # noqa: BLE001
                if self._redis_down_until < now:
                    log.warning("rate limiter: Redis unavailable (%s); counting in process memory", e)
                self._redis_down_until = now + 30
        until = self._cooldowns.get(k, 0.0)
        if until > now:
            return False, max(1, int(until - now + 0.999))
        self._cooldowns[k] = now + seconds
        return True, 0


_limiter: RateLimiter | None = None


def limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from ..config import config
        _limiter = RateLimiter(config.redis_url if config.rate_limit_redis else None)
    return _limiter
