"""Sunset damage signals over HTTP.

    POST /api/v1/webhooks/signals/sentry/{org_slug}    Sentry integration webhook (Sentry-Hook-Signature)
    POST /api/v1/webhooks/signals/generic/{org_slug}   anything else, e.g. an API gateway or log pipeline
                                                       (X-FeatureLedger-Signature: sha256=<hmac of body>)
    GET  /api/v1/damage-signals                        what was received, per feature
    POST /api/v1/damage-signals/{signal_id}/dismiss    "unrelated": stops it blocking a scream test

Secrets: SENTRY_CLIENT_SECRET (your Sentry internal integration's client secret) and SIGNALS_WEBHOOK_SECRET.
In SANDBOX_MODE the shared sandbox secret (X-FeatureLedger-Secret: sandbox) is accepted as well.
One secret per deployment: in a multi-tenant deployment every tenant would need its own (not built yet).
"""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.session import Principal, require
from ..config import config
from ..connectors import webhooks as wh
from ..core.damage import SignalError, match_feature, parse_generic, parse_sentry
from ..core.manifest import ManifestError, parse_manifest
from ..db import org_tx, resolve_org
from ..graph.manifest_store import active_manifest_text
from .deps import current_org, row

log = logging.getLogger("featureledger.signals")
router = APIRouter(prefix="/api/v1", tags=["damage signals"])
MAX_BODY = 1_000_000


def _verify(source: str, request: Request, body: bytes) -> None:
    h = request.headers
    if config.sandbox_mode and h.get("x-featureledger-secret"):
        wh.verify_shared_secret("sandbox", h.get("x-featureledger-secret"))
        return
    if source == "sentry":
        secret = os.environ.get("SENTRY_CLIENT_SECRET", "")
        if not secret:
            raise HTTPException(412, "SENTRY_CLIENT_SECRET is not configured")
        sig = h.get("sentry-hook-signature") or ""
        wh.verify_hub_signature(secret, "sha256=" + sig if sig else None, body)
    else:
        secret = os.environ.get("SIGNALS_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(412, "SIGNALS_WEBHOOK_SECRET is not configured")
        wh.verify_hub_signature(secret, h.get("x-featureledger-signature"), body)


@router.post("/webhooks/signals/{source}/{org_slug}", status_code=202)
async def receive(source: str, org_slug: str, request: Request):
    if source not in ("sentry", "generic"):
        raise HTTPException(404, "unknown signal source")
    org = await resolve_org(org_slug)
    if not org:
        raise HTTPException(404, "unknown organization")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "signal payload too large")
    try:
        _verify(source, request, body)
    except wh.BadSignature as e:
        raise HTTPException(401, f"webhook rejected: {e}") from e
    try:
        payload = json.loads(body or b"{}")
        if not isinstance(payload, dict):
            raise SignalError("expected a JSON object")
        signals = (parse_sentry(request.headers.get("sentry-hook-resource"), payload) if source == "sentry"
                   else parse_generic(payload))
    except (ValueError, SignalError) as e:
        raise HTTPException(422, f"invalid signal payload: {e}") from e
    if not signals:
        return {"accepted": 0, "matched": 0}

    async with org_tx(org) as conn:
        try:
            specs = parse_manifest(await active_manifest_text(conn)).features
        except ManifestError:
            specs = []
        ids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
        matched, new, out = 0, 0, []
        for s in signals:
            key, how = match_feature(s, specs)
            fid = ids.get(key) if key else None
            if fid is None:
                key, how = None, "none"
            matched += fid is not None
            inserted = await conn.fetchval("""
                INSERT INTO damage_signals (org_id, feature_id, source, kind, external_id, title, route, matched_by,
                                            occurrences, occurred_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (org_id, source, external_id) DO UPDATE
                   SET occurrences = greatest(damage_signals.occurrences, EXCLUDED.occurrences)
                RETURNING (xmax = 0)""",
                org, fid, s.source, s.kind, s.external_id, s.title, s.route, how, s.occurrences, s.occurred_at)
            new += bool(inserted)
            out.append({"id": s.external_id, "feature": key, "matched_by": how})
    if matched:
        log.info("damage signals for %s: %d received, %d matched to a feature", org_slug, len(signals), matched)
    return {"accepted": len(signals), "new": new, "matched": matched, "signals": out[:50]}


@router.get("/damage-signals")
async def list_signals(feature_key: str | None = None, include_dismissed: bool = False, limit: int = 200,
                       org: str = Depends(current_org)):
    async with org_tx(org) as conn:
        rows = await conn.fetch("""
            SELECT d.id, f.key AS feature_key, d.source, d.kind, d.external_id, d.title, d.route, d.matched_by,
                   d.occurrences, d.occurred_at, d.received_at, d.dismissed_at, d.dismissed_by, d.dismiss_reason
              FROM damage_signals d LEFT JOIN features f ON f.id = d.feature_id
             WHERE ($1::text IS NULL OR f.key = $1) AND ($2 OR d.dismissed_at IS NULL)
             ORDER BY d.occurred_at DESC LIMIT $3""", feature_key, include_dismissed, max(1, min(limit, 1000)))
    return [row(r) for r in rows]


class DismissIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/damage-signals/{signal_id}/dismiss")
async def dismiss(signal_id: str, body: DismissIn, p: Principal = Depends(require("sunset:approve_cleanup"))):
    """An admin judged the signal unrelated to the sunset; it stops blocking (and stays on record)."""
    async with org_tx(p.org_id) as conn:
        r = await conn.fetchrow("""UPDATE damage_signals SET dismissed_at = now(), dismissed_by = $2, dismiss_reason = $3
                                    WHERE id::text = $1 AND dismissed_at IS NULL
                                RETURNING id, kind, external_id, dismissed_at, dismissed_by, dismiss_reason""",
                                signal_id, p.email, body.reason)
        if not r:
            raise HTTPException(404, "signal not found (or already dismissed)")
    return row(r)
