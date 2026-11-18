from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException


from ..auth.session import Principal, require, require_read


async def current_org(p: Principal = Depends(require_read)) -> str:
    """Any signed-in role may read. The tenant comes from the caller's user record (Phase 4)."""
    return p.org_id


def org_for(permission: str):
    """Dependency: enforce `permission` (app/auth/rbac.py) and return the caller's org id."""
    dep = require(permission)

    async def _org(p: Principal = Depends(dep)) -> str:
        return p.org_id
    _org.__name__ = f"org_for_{permission.replace(':', '_')}"
    return _org


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
