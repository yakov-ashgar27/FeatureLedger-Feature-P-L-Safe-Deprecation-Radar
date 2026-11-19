"""Fail-closed startup: with SANDBOX_MODE=false the API refuses unsafe settings before serving anything."""
import asyncio
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")   # app.main needs FastAPI (installed in CI and the API image)

import app.main as main
from app.config import MIN_SESSION_SECRET, ConfigError, assert_safe_to_start, config, production_problems

GOOD = "x" * MIN_SESSION_SECRET


def cfg(**kw):
    return replace(config, **kw)


def boot(monkeypatch, c):
    """Run the real FastAPI lifespan with `c` as the configuration and a stubbed pool."""
    calls = []

    async def fake_pool():
        calls.append("pool")
    monkeypatch.setattr(main, "assert_safe_to_start", lambda: assert_safe_to_start(c))
    monkeypatch.setattr(main, "init_pool", fake_pool)
    monkeypatch.setattr(main, "close_pool", fake_pool)

    async def run():
        async with main.lifespan(main.app):
            calls.append("serving")
    asyncio.run(run())
    return calls


def test_sandbox_boots_with_any_settings(monkeypatch):
    c = cfg(sandbox_mode=True, demo_mode=True, session_secret=None)
    assert production_problems(c) == []
    assert boot(monkeypatch, c) == ["pool", "serving", "pool"]


def test_production_boots_with_safe_settings(monkeypatch):
    c = cfg(sandbox_mode=False, demo_mode=False, session_secret=GOOD)
    assert production_problems(c) == []
    assert boot(monkeypatch, c) == ["pool", "serving", "pool"]


@pytest.mark.parametrize("overrides,needle", [
    ({"demo_mode": True}, "DEMO_MODE=true"),
    ({"session_secret": None}, "SESSION_SECRET is missing or blank"),
    ({"session_secret": "   "}, "SESSION_SECRET is missing or blank"),
    ({"session_secret": "x" * (MIN_SESSION_SECRET - 1)}, f"SESSION_SECRET is {MIN_SESSION_SECRET - 1} characters"),
])
def test_production_refuses_unsafe_settings(monkeypatch, overrides, needle):
    c = cfg(**{"sandbox_mode": False, "demo_mode": False, "session_secret": GOOD, **overrides})
    with pytest.raises(ConfigError, match="refusing to start with SANDBOX_MODE=false") as e:
        boot(monkeypatch, c)
    assert needle in str(e.value)


def test_pool_is_never_opened_on_refusal(monkeypatch):
    c = cfg(sandbox_mode=False, demo_mode=True, session_secret=None)
    calls = []

    async def fake_pool():
        calls.append("pool")
    monkeypatch.setattr(main, "assert_safe_to_start", lambda: assert_safe_to_start(c))
    monkeypatch.setattr(main, "init_pool", fake_pool)

    async def run():
        async with main.lifespan(main.app):
            pass
    with pytest.raises(ConfigError) as e:
        asyncio.run(run())
    assert calls == [] and str(e.value).count("\n  - ") == 2   # both problems reported at once


def test_env_parsing_is_what_production_sees(monkeypatch):
    from app.config import Config
    monkeypatch.setenv("SANDBOX_MODE", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("SESSION_SECRET", "short")
    problems = production_problems(Config())
    assert len(problems) == 2 and "DEMO_MODE" in problems[0] and "5 characters" in problems[1]
