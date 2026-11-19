"""Zero-login live demo (DEMO_MODE=true): launch, switch role, reset. The demo org is its own tenant."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from ..auth.session import Principal, current_principal, issue_session, principal_from_user
from ..config import config
from ..db import auth_call, org_tx, resolve_org
from ..demo import DEMO_LABEL, reset_demo

log = logging.getLogger("featureledger.demo")
router = APIRouter(prefix="/api/v1/demo", tags=["demo"])
DemoRole = Literal["viewer", "pm_tech_lead", "cs_lead", "admin"]


def _enabled() -> None:
    if not config.demo_mode:
        raise HTTPException(404, "the live demo is off (set DEMO_MODE=true)")


def _cookie(resp: Response, token: str) -> None:
    from .auth import _set_session
    _set_session(resp, token)


async def _sign_in(role: str, resp: Response) -> dict:
    user = await auth_call("auth_dev_user", config.demo_org_slug, role, None)
    if not user:
        raise HTTPException(503, "the demo organization is not ready yet; try again in a moment")
    token = issue_session(user, "demo")
    _cookie(resp, token)
    return {"token": token, "user": principal_from_user(user).public()}


@router.get("/status")
async def status():
    if not config.demo_mode:
        return {"enabled": False}
    org = await resolve_org(config.demo_org_slug)
    engineers = 0
    if org:
        async with org_tx(org) as conn:
            engineers = int(await conn.fetchval("SELECT coalesce(sum(headcount), 0) FROM teams") or 0)
    return {"enabled": True, "ready": bool(org), "slug": config.demo_org_slug, "name": config.demo_org_name,
            "engineers": engineers, "label": DEMO_LABEL.format(name=config.demo_org_name, engineers=engineers or 45),
            "roles": ["viewer", "pm_tech_lead", "cs_lead", "admin"], "default_role": "pm_tech_lead"}


class LaunchIn(BaseModel):
    role: DemoRole = "pm_tech_lead"


@router.post("/launch")
async def launch(response: Response, body: LaunchIn | None = None):
    """No login: a session in the demo org (never in a real tenant)."""
    _enabled()
    return await _sign_in((body or LaunchIn()).role, response)


class SwitchIn(BaseModel):
    role: DemoRole


@router.post("/switch-role")
async def switch_role(body: SwitchIn, response: Response, p: Principal = Depends(current_principal)):
    _enabled()
    if not p.is_demo:
        raise HTTPException(403, {"code": "not_demo", "message": "role switching exists only in the live demo"})
    return await _sign_in(body.role, response)


@router.post("/reset")
async def reset(p: Principal = Depends(current_principal)):
    """Restore the demo org to its seed state. Allowed from a demo session or the FL_API_TOKEN service."""
    _enabled()
    if not (p.is_demo or p.kind == "service"):
        raise HTTPException(403, {"code": "not_demo", "message": "only the live demo can be reset"})
    if p.kind != "service":       # a reset reloads the whole demo: one per cooldown for all visitors together
        from ..core.ratelimit import limiter
        ok, retry = await limiter().cooldown("demo-reset", max(1, config.demo_reset_cooldown_s))
        if not ok:
            raise HTTPException(429, {"code": "rate_limited", "message": f"the demo was just reset; try again in {retry}s"},
                                headers={"Retry-After": str(retry)})
    try:
        out = await reset_demo()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    log.info("demo reset by %s (%s)", p.email, p.role.value)
    return out
