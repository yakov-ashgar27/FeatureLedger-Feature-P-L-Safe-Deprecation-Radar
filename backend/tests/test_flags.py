"""Phase 4 flag providers: LaunchDarkly semantic patch, Statsig gate rules + exposure webhooks, sandbox."""
import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest

from app.flags.base import FlagProviderError, FlagState
from app.flags.launchdarkly import SEMANTIC_PATCH, LaunchDarklyProvider
from app.flags.sandbox import SandboxFlagProvider
from app.flags.statsig import BadStatsigSignature, StatsigProvider, exposures_to_buckets, verify_statsig
from tests.fakes import ScriptedTransport

FLAG = {"key": "legacy-pdf-export", "variations": [{"_id": "var-on", "value": True}, {"_id": "var-off", "value": False}]}


def ld(routes=None):
    t = ScriptedTransport(routes or {("GET", "/api/v2/flags/"): FLAG, ("PATCH", "/api/v2/flags/"): {"_version": 42}})
    return LaunchDarklyProvider(t, "default", "production", "account"), t


def _patch(t):
    return [c for c in t.calls if c["method"] == "PATCH"][-1]


def test_launchdarkly_zero_usage_off_keeps_active_accounts_on():
    p, t = ld()
    ref = asyncio.run(p.set_state("legacy-pdf-export", FlagState.ZERO_USAGE_OFF, ["cus_b", "cus_a", "cus_a"], "dark launch"))
    call = _patch(t)
    assert call["url"] == "/api/v2/flags/default/legacy-pdf-export"
    assert call["headers"]["Content-Type"] == SEMANTIC_PATCH
    body = call["json"]
    assert body["environmentKey"] == "production" and body["comment"] == "dark launch"
    assert body["instructions"] == [
        {"kind": "turnFlagOn"},
        {"kind": "updateFallthroughVariationOrRollout", "variationId": "var-off"},
        {"kind": "replaceTargets", "targets": [{"variationId": "var-on", "contextKind": "account",
                                                "values": ["cus_a", "cus_b"]}]}]
    assert ref["version"] == 42


def test_launchdarkly_fully_off_and_rollback_to_fully_on():
    p, t = ld()
    asyncio.run(p.set_state("f", FlagState.FULLY_OFF, ["ignored"], "scream test"))
    assert _patch(t)["json"]["instructions"][1:] == [
        {"kind": "updateFallthroughVariationOrRollout", "variationId": "var-off"}, {"kind": "replaceTargets", "targets": []}]
    asyncio.run(p.set_state("f", FlagState.FULLY_ON, [], "rollback"))
    assert _patch(t)["json"]["instructions"][1] == {"kind": "updateFallthroughVariationOrRollout", "variationId": "var-on"}


def test_launchdarkly_errors_fail_closed():
    p, _ = ld({("GET", "/api/v2/flags/"): FLAG, ("PATCH", "/api/v2/flags/"): (403, {"message": "Access denied"})})
    with pytest.raises(FlagProviderError, match="403 Access denied"):
        asyncio.run(p.set_state("f", FlagState.FULLY_OFF, [], "x"))
    p, _ = ld({("GET", "/api/v2/flags/"): {"variations": [{"_id": "only", "value": "a"}]}})
    with pytest.raises(FlagProviderError, match="on and an off"):
        asyncio.run(p.set_state("f", FlagState.FULLY_OFF, [], "x"))


def test_launchdarkly_evaluation_series_split_by_variation():
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    series = {"series": [{"time": int(t0.timestamp() * 1000), "0": 3, "1": 40},
                         {"time": int(t0.timestamp() * 1000) + 3_600_000, "1": 12}]}
    p, t = ld({("GET", "/api/v2/flags/"): FLAG, ("GET", "/api/v2/usage/evaluations/"): series})
    buckets = asyncio.run(p.pull_evaluations("legacy-pdf-export", t0, datetime(2026, 11, 2, tzinfo=timezone.utc)))
    assert [(b.on_count, b.off_count) for b in buckets] == [(3, 40), (0, 12)]
    assert buckets[0].bucket_start == t0
    ev = [c for c in t.calls if "/usage/" in c["url"]][0]
    assert ev["url"] == "/api/v2/usage/evaluations/default/production/legacy-pdf-export"
    assert ev["params"]["from"] == int(t0.timestamp() * 1000)


def test_statsig_rules_per_state():
    t = ScriptedTransport({("PATCH", "/gates/"): {"data": {}}})
    p = StatsigProvider(t, "accountID")
    asyncio.run(p.set_state("zapier-v1", FlagState.ZERO_USAGE_OFF, ["acc_2", "acc_1"], "x"))
    body = t.calls[-1]["json"]
    assert t.calls[-1]["url"] == "/gates/zapier-v1" and body["isEnabled"] is True
    cond = body["rules"][0]["conditions"][0]
    assert cond == {"type": "unit_id", "operator": "any", "idType": "accountID", "targetValue": ["acc_1", "acc_2"]}
    asyncio.run(p.set_state("zapier-v1", FlagState.FULLY_OFF, [], "x"))
    assert t.calls[-1]["json"]["rules"] == []
    asyncio.run(p.set_state("zapier-v1", FlagState.FULLY_ON, [], "x"))
    assert t.calls[-1]["json"]["rules"][0]["conditions"] == [{"type": "public"}]
    assert p.push_based and asyncio.run(p.pull_evaluations("zapier-v1", datetime.now(timezone.utc),
                                                           datetime.now(timezone.utc))) == []


def test_statsig_webhook_signature_and_exposure_counting():
    body = json.dumps({"data": [
        {"eventName": "statsig::gate_exposure", "timestamp": 1793000000000, "metadata": {"gate": "zapier-v1", "gateValue": "true"}},
        {"eventName": "statsig::gate_exposure", "timestamp": 1793000060000, "metadata": {"gate": "zapier-v1", "gateValue": "false"}},
        {"eventName": "statsig::gate_exposure", "timestamp": 1793000120000, "metadata": {"gate": "zapier-v1", "gateValue": "false"}},
        {"eventName": "custom_event", "timestamp": 1793000000000, "metadata": {}},
    ]}).encode()
    ts = str(int(time.time() * 1000))
    sig = "v0=" + hmac.new(b"whsec", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    verify_statsig("whsec", sig, ts, body)
    with pytest.raises(BadStatsigSignature):
        verify_statsig("whsec", sig, ts, body + b" ")
    with pytest.raises(BadStatsigSignature, match="stale"):
        verify_statsig("whsec", sig, str(int((time.time() - 3600) * 1000)), body)
    buckets = exposures_to_buckets(body)
    assert [(b.on_count, b.off_count) for b in buckets["zapier-v1"]] == [(1, 2)]


def test_sandbox_provider_records_calls():
    p = SandboxFlagProvider()
    ref = asyncio.run(p.set_state("f", FlagState.ZERO_USAGE_OFF, ["a", "b"], "x"))
    assert ref["keep_on_for"] == 2 and p.calls == [("f", "zero_usage_off", ["a", "b"])]
