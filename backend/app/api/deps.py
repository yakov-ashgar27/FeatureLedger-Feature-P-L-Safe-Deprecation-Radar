from __future__ import annotations

import hmac
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import Header, HTTPException

from ..config import config
from ..db import resolve_org

_org_cache: dict[str, str] = {}


async def current_org(authorization: str | None = Header(default=None),
                      x_org_slug: str | None = Header(default=None)) -> str:
    """Phase 1 auth: optional static bearer token + org slug header (default org in dev)."""
    if config.api_token:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, config.api_token):
            raise HTTPException(401, "invalid or missing bearer token")
    slug = x_org_slug or config.default_org_slug
    if slug not in _org_cache:
        org = await resolve_org(slug)
        if not org:
            raise HTTPException(404, f"unknown organization {slug!r}")
        _org_cache[slug] = org
    return _org_cache[slug]


def parse_period(period: str | None) -> date | None:
    if not period:
        return None
    try:
        y, m = period[:7].split("-")
        return date(int(y), int(m), 1)
    except ValueError as e:
        raise HTTPException(422, "period must be YYYY-MM") from e


def plain(v: Any) -> Any:
    """asyncpg values -> JSON-friendly."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


def row(r) -> dict[str, Any]:
    return {k: plain(v) for k, v in dict(r).items()}
