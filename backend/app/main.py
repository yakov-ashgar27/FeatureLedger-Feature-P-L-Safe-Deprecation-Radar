from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, auth, demo, flags, graph, guard, imports, integrations, pnl, reports, sunset
from .config import assert_safe_to_start, config
from .core.ratelimit import client_address, client_key, limiter, rule_for
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


# Every response. The API serves JSON only, so its CSP forbids everything (the Swagger pages are exempt).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


@app.middleware("http")
async def harden(request: Request, call_next):
    path = request.url.path
    rule = rule_for(request.method, path) if config.rate_limit_enabled else None
    if rule is not None:
        addr = client_address(request.client.host if request.client else None,
                              request.headers.get("x-forwarded-for"), config.trusted_proxy_hops)
        auth = request.headers.get("authorization", "")
        key = client_key(rule, addr, auth[7:] if auth[:7].lower() == "bearer " else None,
                         request.cookies.get("fl_session"))
        ok, retry = await limiter().hit(f"{rule.name}:{key}", rule.limit, rule.window_s)
        if not ok:
            resp = JSONResponse({"detail": {"code": "rate_limited", "limit": rule.limit, "window_s": rule.window_s,
                                            "message": f"too many requests; retry in {retry}s"}},
                                status_code=429, headers={"Retry-After": str(retry)})
            resp.headers.update(SECURITY_HEADERS)
            return resp
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = API_CSP
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")     # financial data: never in shared caches
    if config.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


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
