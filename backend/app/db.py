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
