"""Statsig adapter: Console API gate rules; evaluation counts arrive as exposure events by webhook.

    set_state  PATCH /console/v1/gates/{gate}   isEnabled + rules
                 fully_on        one rule: public, pass 100%
                 zero_usage_off  one rule: unit_id any-of keep_on_for (idType STATSIG_ID_TYPE), pass 100%
                 fully_off       no rules -> every check fails (gate stays enabled so exposures still log)
    exposures  POST /api/v1/flags/webhooks/statsig?org=<slug>   (Statsig Event Webhook,
               X-Statsig-Signature: v0=HMAC_SHA256(secret, "v0:{ts}:{body}"))
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..connectors.base import Transport
from .base import EvalBucket, FlagState, check_response


class StatsigProvider:
    name = "statsig"
    push_based = True

    def __init__(self, transport: Transport, id_type: str = "accountID"):
        self.t, self.id_type = transport, id_type

    def rules(self, state: FlagState, keep_on_for: list[str]) -> list[dict[str, Any]]:
        if state is FlagState.FULLY_ON:
            return [{"name": "featureledger: everyone", "passPercentage": 100, "conditions": [{"type": "public"}]}]
        if state is FlagState.ZERO_USAGE_OFF and keep_on_for:
            return [{"name": "featureledger: accounts active in the last 90 days", "passPercentage": 100,
                     "conditions": [{"type": "unit_id", "operator": "any", "idType": self.id_type,
                                     "targetValue": sorted(set(keep_on_for))}]}]
        return []

    async def set_state(self, flag_key: str, state: FlagState, keep_on_for: list[str], comment: str) -> dict[str, Any]:
        body = {"isEnabled": True, "rules": self.rules(state, keep_on_for)}
        r = await self.t.request("PATCH", f"/gates/{flag_key}", json_body=body)
        check_response(self.name, f"set {flag_key} {state.value}", r.status, r.body)
        return {"gate": flag_key, "rules": len(body["rules"])}

    async def pull_evaluations(self, flag_key: str, since: datetime, until: datetime) -> list[EvalBucket]:
        return []


class BadStatsigSignature(ValueError):
    pass


def verify_statsig(secret: str, signature: str | None, timestamp: str | None, body: bytes,
                   now: float | None = None, tolerance: int = 300) -> None:
    if not signature or not timestamp:
        raise BadStatsigSignature("missing signature headers")
    if abs((now or time.time()) - int(timestamp) / (1000 if len(timestamp) > 11 else 1)) > tolerance:
        raise BadStatsigSignature("stale timestamp")
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise BadStatsigSignature("signature mismatch")


def exposures_to_buckets(body: bytes) -> dict[str, list[EvalBucket]]:
    """Statsig event-webhook batch -> hourly on/off counts per gate."""
    doc = json.loads(body or b"[]")
    events = doc.get("data", []) if isinstance(doc, dict) else doc
    counts: dict[tuple[str, datetime], list[int]] = defaultdict(lambda: [0, 0])
    for e in events:
        if e.get("eventName") != "statsig::gate_exposure":
            continue
        md = e.get("metadata") or {}
        gate = md.get("gate")
        if not gate:
            continue
        ts = datetime.fromtimestamp(int(e.get("timestamp") or e.get("timeStamp") or 0) / 1000, tz=timezone.utc)
        hour = ts.replace(minute=0, second=0, microsecond=0)
        on = str(md.get("gateValue", md.get("value", "false"))).lower() == "true"
        counts[(gate, hour)][0 if on else 1] += 1
    out: dict[str, list[EvalBucket]] = defaultdict(list)
    for (gate, hour), (on, off) in sorted(counts.items()):
        out[gate].append(EvalBucket(hour, on, off))
    return dict(out)
