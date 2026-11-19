"""Sunset damage signals: parsing Sentry / generic payloads, attribution to features, redaction, guards."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.damage import Signal, SignalError, clean_route, match_feature, parse_generic, parse_sentry, redact
from app.core.manifest import FeatureSpec
from app.core.sunset import PlanView, SunsetState, TransitionError, advance

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
SPECS = [
    FeatureSpec("legacy-pdf-export", "Legacy PDF", paths=("services/export/pdf_v1/**",), api_routes=("/api/v1/export-pdf*",)),
    FeatureSpec("pdf-export-v2", "PDF v2", paths=("services/export/**",), api_routes=("/api/v1/export*",)),
    FeatureSpec("platform", "Platform", paths=("services/**",), shared_overhead=True),
]


def sig(**kw):
    return Signal(**{"source": "generic", "kind": "error", "external_id": "x", "occurred_at": NOW, **kw})


def test_attribution_prefers_tag_then_most_specific_route_then_most_specific_path():
    assert match_feature(sig(feature_tag="pdf-export-v2", route="/api/v1/export-pdf/1"), SPECS) == ("pdf-export-v2", "tag")
    assert match_feature(sig(route="/api/v1/export-pdf/:id"), SPECS) == ("legacy-pdf-export", "route")
    assert match_feature(sig(route="/api/v1/exports/:id"), SPECS) == ("pdf-export-v2", "route")
    frames = ("/srv/app/services/export/pdf_v1/render.py",)
    assert match_feature(sig(frames=frames), SPECS) == ("legacy-pdf-export", "path")
    assert match_feature(sig(frames=("lib/python3.12/site-packages/x.py",)), SPECS) == (None, "none")
    assert match_feature(sig(feature_tag="nope"), SPECS) == (None, "none")
    # shared platform overhead is never blamed
    assert match_feature(sig(frames=("services/auth/login.py",)), SPECS) == (None, "none")


def test_nothing_identifying_is_kept():
    assert redact("Failed for jane.doe@acme.com card 4111 1111 1111 1111") == "Failed for [email] card [number]"
    assert clean_route("https://api.acme.com/api/v1/export-pdf/123456/x?token=secret#a") == "/api/v1/export-pdf/:id/x"
    assert clean_route("/v1/docs/3f2b8c1e-9a7d-4c1b-8e2f-1a2b3c4d5e6f") == "/v1/docs/:id"
    assert clean_route("not a route") is None


def test_sentry_error_event():
    payload = {"action": "created", "data": {"error": {
        "event_id": "abc", "title": "TypeError for bob@x.io", "datetime": "2026-11-18T10:00:00Z",
        "tags": [["transaction", "/api/v1/export-pdf/77"], ["feature", "legacy-pdf-export"]],
        "exception": {"values": [{"stacktrace": {"frames": [
            {"filename": "lib/x.py", "in_app": False}, {"filename": "services/export/pdf_v1/a.py"}]}}]}}}}
    [s] = parse_sentry("error", payload)
    assert (s.external_id, s.kind, s.feature_tag, s.route) == ("event:abc", "error", "legacy-pdf-export", "/api/v1/export-pdf/:id")
    assert s.title == "TypeError for [email]" and s.frames == ("services/export/pdf_v1/a.py",)
    assert s.occurred_at == datetime(2026, 11, 18, 10, tzinfo=timezone.utc)


def test_sentry_issue_and_noise():
    [s] = parse_sentry("issue", {"action": "created", "data": {"issue": {"id": "42", "title": "Boom", "count": "7",
                                                                       "culprit": "/api/v1/export-pdf"}}})
    assert (s.external_id, s.occurrences, s.route) == ("issue:42", 7, "/api/v1/export-pdf")
    assert parse_sentry("issue", {"action": "resolved", "data": {"issue": {"id": "42"}}}) == []
    assert parse_sentry("installation", {"action": "created", "data": {"installation": {}}}) == []


def test_generic_signals_are_validated():
    [s] = parse_generic({"signals": [{"id": "gw-1", "kind": "deprecated_call", "route": "/api/v1/export-pdf?a=1",
                                      "count": 12, "occurred_at": "2026-11-18T00:00:00Z"}]})
    assert (s.kind, s.occurrences, s.route) == ("deprecated_call", 12, "/api/v1/export-pdf")
    for bad in ({}, {"signals": []}, {"signals": [{"id": "a", "kind": "warning"}]}, {"signals": [{"kind": "error"}]},
                {"signals": [{"id": "a", "kind": "error", "count": "many"}]}, {"signals": [{"id": "a"}] * 501}):
        with pytest.raises(SignalError):
            parse_generic(bad)


def _canary(phase):
    started = NOW - timedelta(days=60)
    canary = {"phase": phase, "started_at": started.isoformat()}
    if phase == "fully_off":
        canary["fully_off_at"] = (NOW - timedelta(days=40)).isoformat()
    return PlanView(state=SunsetState.CANARY_DARK_LAUNCH, feature_is_regulatory=False, flag_key="f", scream_test_days=30,
                    state_entered_at=started, canary_phase=phase, canary=canary)


def test_damage_blocks_the_scream_test():
    with pytest.raises(TransitionError) as e:
        advance(_canary("zero_usage_off"), {"errors_since_canary": 2}, now=NOW)
    assert e.value.code == "errors_during_canary"
    assert advance(_canary("zero_usage_off"), {"errors_since_canary": 0}, now=NOW).action == "canary_promote"
    ok = {"cleanup_pr": {"title": "x"}}
    with pytest.raises(TransitionError) as e:
        advance(_canary("fully_off"), {**ok, "errors_since_off": 1}, now=NOW)
    assert e.value.code == "errors_after_off"
    with pytest.raises(TransitionError) as e:
        advance(_canary("fully_off"), {**ok, "deprecated_calls_since_off": 5}, now=NOW)
    assert e.value.code == "deprecated_route_called" and "5 call(s)" in str(e.value)
    assert advance(_canary("fully_off"), ok, now=NOW).to_state is SunsetState.DEAD_CODE_PR_OPENED
