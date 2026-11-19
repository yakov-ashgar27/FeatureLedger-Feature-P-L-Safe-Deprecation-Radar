"""asyncpg pool + tenant-scoped transactions.

Every request runs inside `org_tx(org_id)`, which sets `app.org_id` for the transaction only
(`set_config(..., true)`), so Row-Level Security filters every statement, and a pooled connection
can never carry one tenant's id into another request.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from .config import config

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    # pgvector: bind and read vectors as their text form '[0.1,0.2,...]' (no extra client library)
    if await conn.fetchval("SELECT to_regtype('public.vector') IS NOT NULL"):
        await conn.set_type_codec("vector", encoder=str, decoder=str, schema="public", format="text")


async def init_pool(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # jit=off: short OLTP statements (and asyncpg's type introspection) must not pay LLVM compile time
        _pool = await asyncpg.create_pool(dsn or config.database_url, min_size=1, max_size=10, init=_init_conn,
                                          server_settings={"jit": "off"})
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def org_tx(org_id: str) -> AsyncIterator[asyncpg.Connection]:
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
            yield conn


async def resolve_org(slug: str) -> str | None:
    pool = await init_pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT resolve_org_slug($1)", slug)
    return str(v) if v else None


async def auth_call(fn: str, *args) -> dict | None:
    """Call one of the pre-tenant SECURITY DEFINER auth functions (0006); returns one user row or None."""
    pool = await init_pool()
    placeholders = ", ".join(f"${i + 1}" for i in range(len(args)))
    async with pool.acquire() as conn:
        r = await conn.fetchrow(f"SELECT * FROM {fn}({placeholders})", *args)
    return dict(r) if r else None


async def integration_org(integration_id: str) -> str | None:
    pool = await init_pool()
    async with pool.acquire() as conn:
        try:
            v = await conn.fetchval("SELECT integration_org($1::uuid)", integration_id)
        except asyncpg.DataError:
            return None
    return str(v) if v else None


async def org_is_demo(conn) -> bool:
    """Inside an org transaction: is this tenant the live-demo org? (Demo orgs never touch real vendors.)"""
    return bool(await conn.fetchval("SELECT coalesce(bool_or(is_demo), false) FROM organizations"))
