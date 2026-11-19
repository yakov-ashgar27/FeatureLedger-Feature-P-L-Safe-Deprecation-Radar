"""Rate-limit rules, client identification, counters, cooldowns, and the external-LLM consent switch."""
import asyncio
import logging

from app.core.ratelimit import (API, DEMO_SESSION, LOGIN, SSO, WEBHOOK, MemoryCounter, RateLimiter, client_address,
                                client_key, rule_for)
from app.graph.llm_policy import external_llm_allowed


def test_rules():
    assert rule_for("POST", "/api/v1/auth/dev-login") is LOGIN
    assert rule_for("GET", "/api/v1/auth/oidc/callback") is SSO
    assert rule_for("POST", "/api/v1/demo/launch") is DEMO_SESSION
    assert rule_for("POST", "/api/v1/demo/switch-role") is DEMO_SESSION
    assert rule_for("POST", "/api/v1/webhooks/stripe/abc") is WEBHOOK
    assert rule_for("GET", "/api/v1/pnl") is API
    assert rule_for("GET", "/health") is None and rule_for("GET", "/docs") is None


def test_client_address_trusts_only_the_configured_hops():
    assert client_address("10.0.0.5", "1.2.3.4", 0) == "10.0.0.5"                 # no proxy trusted: header ignored
    assert client_address("10.0.0.5", "6.6.6.6, 1.2.3.4", 1) == "1.2.3.4"         # forged left entries are ignored
    assert client_address("10.0.0.5", "6.6.6.6, 1.2.3.4, 10.0.0.9", 2) == "1.2.3.4"
    assert client_address("10.0.0.5", "1.2.3.4", 5) == "1.2.3.4"
    assert client_address(None, None, 1) == "unknown"


def test_signed_in_traffic_is_keyed_by_session_and_never_exposes_the_token():
    k = client_key(API, "1.2.3.4", "secret-token", None)
    assert k.startswith("s:") and "secret-token" not in k
    assert client_key(API, "1.2.3.4", None, "secret-token") == k
    assert client_key(API, "1.2.3.4", None, None) == "a:1.2.3.4"
    assert client_key(LOGIN, "1.2.3.4", "secret-token", None) == "a:1.2.3.4"       # login limits stay per address


def test_memory_counter_windows():
    c = MemoryCounter()
    assert [c.hit("k", 60, 120.0)[0] for _ in range(3)] == [1, 2, 3]
    assert c.hit("k", 60, 179.9)[0] == 4
    assert c.hit("k", 60, 180.0) == (1, 240.0)                                      # new window


def test_limiter_without_redis_blocks_after_the_limit_and_cools_down():
    async def go():
        rl = RateLimiter(None)
        res = [await rl.hit("x", 3, 60) for _ in range(4)]
        assert [ok for ok, _ in res] == [True, True, True, False] and 1 <= res[-1][1] <= 60
        assert await rl.cooldown("reset", 30) == (True, 0)
        ok, wait = await rl.cooldown("reset", 30)
        assert not ok and 1 <= wait <= 30
    asyncio.run(go())


def test_limiter_falls_back_to_memory_when_redis_is_down(caplog):
    async def go():
        rl = RateLimiter("redis://127.0.0.1:1/0")                                    # nothing listens there
        with caplog.at_level(logging.WARNING, "featureledger.ratelimit"):
            assert (await rl.hit("y", 1, 60))[0] is True
            assert (await rl.hit("y", 1, 60))[0] is False
        assert "Redis unavailable" in caplog.text or "redis package not installed" in caplog.text
    asyncio.run(go())


def test_external_llm_needs_explicit_consent():
    assert not external_llm_allowed({})
    assert not external_llm_allowed({"LLM_API_KEY": "sk-x"})
    assert not external_llm_allowed({"LLM_API_KEY": "sk-x", "LLM_ALLOW_EXTERNAL": "no"})
    assert external_llm_allowed({"LLM_API_KEY": "sk-x", "LLM_ALLOW_EXTERNAL": "true"})
    assert not external_llm_allowed({"LLM_ALLOW_EXTERNAL": "true"})


def test_default_backends_stay_local_without_consent():
    from app.graph.embeddings import HashingEmbedder, default_embedder
    from app.graph.semantic_classifier import TemplateRationale, default_explainer
    env = {"LLM_API_KEY": "sk-x"}
    assert isinstance(default_embedder(env), HashingEmbedder)
    assert isinstance(default_explainer(env), TemplateRationale)
    env["LLM_ALLOW_EXTERNAL"] = "true"
    assert not isinstance(default_embedder(env), HashingEmbedder)
