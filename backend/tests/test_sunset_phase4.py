"""Phase 4 guard semantics on the pure state machine (the HTTP versions live in test_phase4_api.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.rbac import ADVANCE_PERMISSION, can
from app.core.sunset import PlanView, SunsetState, TransitionError, advance, cancel, rollback

NOW = datetime(2026, 11, 18, 9, tzinfo=timezone.utc)


def canary(days_off: int, **kw) -> PlanView:
    off = NOW - timedelta(days=days_off)
    return PlanView(state=SunsetState.CANARY_DARK_LAUNCH, feature_is_regulatory=False, flag_key="zapier-v1",
                    scream_test_days=14, state_entered_at=off, canary_phase="fully_off",
                    canary={"phase": "fully_off", "fully_off_at": off.isoformat(), "rollbacks": kw.pop("rollbacks", 0)},
                    **kw)


QUIET = {"flag_on_evaluations": 0, "tickets_since_off": 0, "cleanup_pr": {"generated_on_transition": True}}


@pytest.mark.parametrize("ctx,code", [
    ({**QUIET, "flag_on_evaluations": 1}, "flag_still_served"),
    ({**QUIET, "tickets_since_off": 2}, "customer_screamed"),
])
def test_any_on_evaluation_or_ticket_blocks_the_cleanup_pr(ctx, code):
    with pytest.raises(TransitionError) as e:
        advance(canary(20), ctx, now=NOW)
    assert e.value.code == code


def test_scream_test_must_run_its_full_length():
    with pytest.raises(TransitionError) as e:
        advance(canary(13), QUIET, now=NOW)
    assert e.value.code == "scream_test_running" and "1 day(s)" in str(e.value)
    assert advance(canary(14), QUIET, now=NOW).to_state is SunsetState.DEAD_CODE_PR_OPENED


def test_zero_usage_off_promotes_to_fully_off_in_place():
    p = canary(0)
    p.canary_phase, p.canary = "zero_usage_off", {"phase": "zero_usage_off"}
    res = advance(p, {}, now=NOW)
    assert (res.from_state, res.to_state, res.action) == (SunsetState.CANARY_DARK_LAUNCH,) * 2 + ("canary_promote",)
    assert res.updates["canary_phase"] == "fully_off" and res.updates["canary"]["fully_off_at"] == NOW.isoformat()


def test_rollback_reenables_the_flag_and_counts():
    res = rollback(canary(3, rollbacks=1), "customer escalation", now=NOW)
    assert res.to_state is SunsetState.CUSTOMER_OUTREACH and res.action == "rollback"
    assert res.updates["flag_action"] == "re_enable" and res.updates["canary"]["rollbacks"] == 2
    pr_open = canary(20)
    pr_open.state = SunsetState.DEAD_CODE_PR_OPENED
    assert rollback(pr_open, "regression", now=NOW).to_state is SunsetState.CUSTOMER_OUTREACH
    assert cancel(canary(3), "nope").updates["flag_action"] == "re_enable"


def test_only_admins_can_flip_flags_or_open_the_pr():
    for role in ("viewer", "pm_tech_lead", "cs_lead"):
        assert not can(role, ADVANCE_PERMISSION[SunsetState.CUSTOMER_OUTREACH])
        assert not can(role, ADVANCE_PERMISSION[SunsetState.CANARY_DARK_LAUNCH])
    assert can("admin", ADVANCE_PERMISSION[SunsetState.CANARY_DARK_LAUNCH])
    assert can("cs_lead", ADVANCE_PERMISSION[SunsetState.BLAST_RADIUS_CHECKED])
    assert can("pm_tech_lead", ADVANCE_PERMISSION[SunsetState.CANDIDATE])
