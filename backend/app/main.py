from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import admin, auth, demo, flags, graph, guard, imports, integrations, pnl, reports, sunset
from .config import assert_safe_to_start, config
from .db import close_pool, init_pool, org_tx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    assert_safe_to_start()            # before any connection is opened or any route is served
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="FeatureLedger API", version="0.5.0", lifespan=lifespan,
              description="Feature-level P&L and Safe Deprecation Radar")
# The dashboard reaches the API through its own origin (Next.js rewrite), so browsers never need CORS;
# this only serves tools on other origins that send a bearer token.
app.add_middleware(CORSMiddleware, allow_origins=list(config.cors_origins), allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "PATCH"],
                   allow_headers=["Authorization", "Content-Type", "X-Org-Slug", "X-FL-CSRF"])
app.include_router(auth.router)
app.include_router(flags.router)
app.include_router(pnl.router)
app.include_router(sunset.router)
app.include_router(admin.router)
app.include_router(imports.router)
app.include_router(integrations.router)
app.include_router(graph.router)
app.include_router(guard.router)
app.include_router(reports.router)
app.include_router(demo.router)


@app.get("/health")
async def health():
    from .db import resolve_org
    org = await resolve_org(config.default_org_slug)
    if org:
        async with org_tx(org) as conn:
            await conn.fetchval("SELECT 1")
    return {"status": "ok", "default_org": bool(org)}
