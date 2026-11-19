"""Safe Sunset orchestrator: a guarded state machine.

    candidate_for_sunset -> blast_radius_checked -> customer_outreach -> canary_dark_launch
        -> dead_code_pr_opened -> archived
    rollback:  canary_dark_launch | dead_code_pr_opened -> customer_outreach  (flag re-enabled)
    cancel:    any non-terminal state -> cancelled

Scream-test evidence that blocks progress: 'on' flag evaluations, support tickets, and damage signals
(production errors and calls to the feature's deprecated API routes, app/core/damage.py).

Every transition is a pure function of (plan, context) -> TransitionResult, so the API layer only
persists the result + an audit row. Guards encode the "never delete blindly" rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class SunsetState(str, Enum):
    CANDIDATE = "candidate_for_sunset"
    BLAST_RADIUS_CHECKED = "blast_radius_checked"
    CUSTOMER_OUTREACH = "customer_outreach"
    CANARY_DARK_LAUNCH = "canary_dark_launch"
    DEAD_CODE_PR_OPENED = "dead_code_pr_opened"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"


ORDER = [SunsetState.CANDIDATE, SunsetState.BLAST_RADIUS_CHECKED, SunsetState.CUSTOMER_OUTREACH,
         SunsetState.CANARY_DARK_LAUNCH, SunsetState.DEAD_CODE_PR_OPENED, SunsetState.ARCHIVED]
TERMINAL = {SunsetState.ARCHIVED, SunsetState.CANCELLED}
ROLLBACK_FROM = {SunsetState.CANARY_DARK_LAUNCH, SunsetState.DEAD_CODE_PR_OPENED}
HIGH_RISK = 0.5           # churn_risk at/above which CS sign-off is mandatory
OUTREACH_GRACE_DAYS = 14  # unacknowledged accounts block the canary for this long


class TransitionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class PlanView:
    state: SunsetState
    feature_is_regulatory: bool
    flag_key: str | None
    scream_test_days: int
    state_entered_at: datetime
    canary_phase: str | None = None
    blast_radius: dict[str, Any] | None = None
    impact_dossier: dict[str, Any] | None = None
    canary: dict[str, Any] | None = None
    cleanup_pr: dict[str, Any] | None = None


@dataclass
class TransitionResult:
    from_state: SunsetState
    to_state: SunsetState
    action: str
    updates: dict[str, Any] = field(default_factory=dict)


def _next(state: SunsetState) -> SunsetState:
    return ORDER[ORDER.index(state) + 1]


def advance(plan: PlanView, ctx: dict[str, Any], now: datetime | None = None) -> TransitionResult:
    """Move the plan one step forward if its guard passes. `ctx` carries fresh evidence."""
    now = now or datetime.now(timezone.utc)
    s = plan.state
    if s in TERMINAL:
        raise TransitionError("terminal", f"plan is {s.value}")
    if plan.feature_is_regulatory:
        raise TransitionError("regulatory", "regulatory features cannot be sunset through FeatureLedger")
    to = _next(s)

    if s is SunsetState.CANDIDATE:
        br = ctx.get("blast_radius")
        if not br or "inbound_references" not in br:
            raise TransitionError("missing_blast_radius", "compute the technical blast radius first")
        unresolved = [r for r in br["inbound_references"] if not r.get("acknowledged")]
        if unresolved:
            raise TransitionError("unacknowledged_dependencies",
                                  f"{len(unresolved)} inbound reference(s) must be acknowledged by an owner")
        return TransitionResult(s, to, "transition", {"blast_radius": br})

    if s is SunsetState.BLAST_RADIUS_CHECKED:
        dossier = ctx.get("impact_dossier")
        if not dossier or "accounts" not in dossier:
            raise TransitionError("missing_dossier", "generate the account impact dossier first")
        risky = [a for a in dossier["accounts"] if a.get("churn_risk", 0) >= HIGH_RISK]
        if risky and not ctx.get("cs_signoff"):
            raise TransitionError("cs_signoff_required",
                                  f"{len(risky)} high-risk account(s): Customer Success sign-off required")
        return TransitionResult(s, to, "transition", {"impact_dossier": dossier})

    if s is SunsetState.CUSTOMER_OUTREACH:
        if not plan.flag_key:
            raise TransitionError("no_flag", "a feature flag is required for a reversible dark launch")
        accounts = (plan.impact_dossier or {}).get("accounts", [])
        pending = [a for a in accounts if not a.get("acknowledged")]
        waited = now - plan.state_entered_at >= timedelta(days=OUTREACH_GRACE_DAYS)
        if pending and not waited:
            raise TransitionError("outreach_pending",
                                  f"{len(pending)} account(s) not yet acknowledged; wait or collect replies")
        canary = {"phase": "zero_usage_off", "started_at": now.isoformat(), "rollbacks": 0}
        return TransitionResult(s, to, "transition", {"canary": canary, "canary_phase": "zero_usage_off"})

    if s is SunsetState.CANARY_DARK_LAUNCH:
        canary = dict(plan.canary or {})
        if plan.canary_phase == "zero_usage_off":
            if ctx.get("errors_since_canary", 0) > 0:
                raise TransitionError("errors_during_canary",
                                      f"{ctx['errors_since_canary']} production error(s) attributed to the feature since "
                                      "the canary started: investigate (or dismiss as unrelated) before going fully off")
            # promote to 100% off; the scream-test clock starts now. Stays in the same state.
            canary.update(phase="fully_off", fully_off_at=now.isoformat())
            return TransitionResult(s, s, "canary_promote", {"canary": canary, "canary_phase": "fully_off"})
        fully_off_at = datetime.fromisoformat(canary["fully_off_at"])
        quiet_days = (now - fully_off_at).days
        if quiet_days < plan.scream_test_days:
            raise TransitionError("scream_test_running",
                                  f"{plan.scream_test_days - quiet_days} day(s) of scream test remaining")
        if ctx.get("flag_on_evaluations", 0) > 0:
            raise TransitionError("flag_still_served", "flag served the 'on' variation during the scream test")
        if ctx.get("tickets_since_off", 0) > 0:
            raise TransitionError("customer_screamed", "support tickets referenced the feature during the test")
        if ctx.get("errors_since_off", 0) > 0:
            raise TransitionError("errors_after_off", f"{ctx['errors_since_off']} production error(s) attributed to the "
                                  "feature since it was switched off")
        if ctx.get("deprecated_calls_since_off", 0) > 0:
            raise TransitionError("deprecated_route_called", f"{ctx['deprecated_calls_since_off']} call(s) to the "
                                  "feature's API routes since it was switched off: someone still depends on it")
        pr = ctx.get("cleanup_pr")
        if not pr:
            raise TransitionError("missing_cleanup_pr", "generate the dead-code PR outline first")
        return TransitionResult(s, to, "transition", {"cleanup_pr": pr})

    if s is SunsetState.DEAD_CODE_PR_OPENED:
        if not ctx.get("pr_merged"):
            raise TransitionError("pr_not_merged", "the cleanup PR must be merged before archiving")
        return TransitionResult(s, to, "transition", {})

    raise TransitionError("invalid", f"no forward transition from {s.value}")


def rollback(plan: PlanView, reason: str, now: datetime | None = None) -> TransitionResult:
    now = now or datetime.now(timezone.utc)
    if plan.state not in ROLLBACK_FROM:
        raise TransitionError("not_rollbackable", f"cannot roll back from {plan.state.value}")
    canary = dict(plan.canary or {})
    canary.update(phase=None, rolled_back_at=now.isoformat(), rollback_reason=reason,
                  rollbacks=int(canary.get("rollbacks", 0)) + 1)
    return TransitionResult(plan.state, SunsetState.CUSTOMER_OUTREACH, "rollback",
                            {"canary": canary, "canary_phase": None, "flag_action": "re_enable"})


def cancel(plan: PlanView, reason: str) -> TransitionResult:
    if plan.state in TERMINAL:
        raise TransitionError("terminal", f"plan is already {plan.state.value}")
    updates: dict[str, Any] = {"reason_cancelled": reason}
    if plan.state in ROLLBACK_FROM:
        updates["flag_action"] = "re_enable"
    return TransitionResult(plan.state, SunsetState.CANCELLED, "cancel", updates)
