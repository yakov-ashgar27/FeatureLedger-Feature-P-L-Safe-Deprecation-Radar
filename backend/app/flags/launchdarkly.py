"""LaunchDarkly adapter: semantic-patch targeting + the evaluation usage API.

    set_state   GET  /api/v2/flags/{project}/{flag}              -> variation ids (true = on)
                PATCH /api/v2/flags/{project}/{flag}              Content-Type: application/json;
                      domain-model=launchdarkly.semanticpatch    turnFlagOn, fallthrough, replaceTargets
    evaluations GET  /api/v2/usage/evaluations/{project}/{env}/{flag}?from=&to=   hourly series per variation

Targeting is keyed by account (context kind LAUNCHDARKLY_CONTEXT_KIND, default 'account').
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..connectors.base import Transport
from .base import EvalBucket, FlagProviderError, FlagState, check_response

SEMANTIC_PATCH = "application/json; domain-model=launchdarkly.semanticpatch"


class LaunchDarklyProvider:
    name = "launchdarkly"
    push_based = False

    def __init__(self, transport: Transport, project: str, environment: str, context_kind: str = "account"):
        self.t, self.project, self.env, self.kind = transport, project, environment, context_kind

    async def _variations(self, flag_key: str) -> tuple[str, str, int]:
        r = await self.t.request("GET", f"/api/v2/flags/{self.project}/{flag_key}", params={"env": self.env})
        check_response(self.name, f"read flag {flag_key}", r.status, r.body)
        vs = r.body.get("variations") or []
        on = next((i for i, v in enumerate(vs) if v.get("value") is True), None)
        off = next((i for i, v in enumerate(vs) if v.get("value") is False), None)
        if on is None or off is None:
            if len(vs) < 2:
                raise FlagProviderError(f"launchdarkly flag {flag_key} needs an on and an off variation")
            on, off = 0, 1     # multivariate: first = on, second = off (documented convention)
        return vs[on]["_id"], vs[off]["_id"], on

    def instructions(self, state: FlagState, on_id: str, off_id: str, keep_on_for: list[str]) -> list[dict[str, Any]]:
        serve = on_id if state is FlagState.FULLY_ON else off_id
        targets = ([{"variationId": on_id, "contextKind": self.kind, "values": sorted(set(keep_on_for))}]
                   if state is FlagState.ZERO_USAGE_OFF and keep_on_for else [])
        return [{"kind": "turnFlagOn"},
                {"kind": "updateFallthroughVariationOrRollout", "variationId": serve},
                {"kind": "replaceTargets", "targets": targets}]

    async def set_state(self, flag_key: str, state: FlagState, keep_on_for: list[str], comment: str) -> dict[str, Any]:
        on_id, off_id, _ = await self._variations(flag_key)
        body = {"environmentKey": self.env, "comment": comment,
                "instructions": self.instructions(state, on_id, off_id, keep_on_for)}
        r = await self.t.request("PATCH", f"/api/v2/flags/{self.project}/{flag_key}", json_body=body,
                                 headers={"Content-Type": SEMANTIC_PATCH})
        check_response(self.name, f"set {flag_key} {state.value}", r.status, r.body)
        return {"version": (r.body or {}).get("_version"), "environment": self.env, "on_variation": on_id,
                "off_variation": off_id}

    async def pull_evaluations(self, flag_key: str, since: datetime, until: datetime) -> list[EvalBucket]:
        _, _, on_index = await self._variations(flag_key)
        r = await self.t.request("GET", f"/api/v2/usage/evaluations/{self.project}/{self.env}/{flag_key}",
                                 params={"from": int(since.timestamp() * 1000), "to": int(until.timestamp() * 1000),
                                         "tz": "UTC"})
        check_response(self.name, f"evaluations {flag_key}", r.status, r.body)
        out = []
        for point in (r.body or {}).get("series", []):
            counts = {k: int(v or 0) for k, v in point.items() if k != "time"}
            on = counts.get(str(on_index), 0)
            out.append(EvalBucket(datetime.fromtimestamp(point["time"] / 1000, tz=timezone.utc), on,
                                  sum(counts.values()) - on))
        return out
