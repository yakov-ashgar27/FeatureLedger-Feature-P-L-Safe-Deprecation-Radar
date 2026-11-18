from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import admin, pnl, sunset
from .config import config
from .db import close_pool, init_pool, org_tx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="FeatureLedger API", version="0.1.0", lifespan=lifespan,
              description="Feature-level P&L and Safe Deprecation Radar")
app.add_middleware(CORSMiddleware, allow_origins=list(config.cors_origins), allow_credentials=False,
                   allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "X-Org-Slug"])
app.include_router(pnl.router)
app.include_router(sunset.router)
app.include_router(admin.router)


@app.get("/health")
async def health():
    from .db import resolve_org
    org = await resolve_org(config.default_org_slug)
    if org:
        async with org_tx(org) as conn:
            await conn.fetchval("SELECT 1")
    return {"status": "ok", "default_org": bool(org)}
