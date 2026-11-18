"""Engineering effort model: relative per-PR effort, calibrated to real team capacity.

    H_raw(pr) = (0.5 + 1.2 * ln(1 + churn / 20))           size: log-damped, typo ~0.6h
              * (1 + 0.15 * min(review_rounds, 6))          rework from changes-requested cycles
              * (1 + 0.05 * min(files_changed - 1, 20))     coordination across files
              * type_multiplier(kind, priority)             bug/hotfix/incident tax
              + incident_response_hours                     aggregate on-call time (team level)

    churn = additions + 0.5 * deletions   (generated/vendored files are excluded at ingestion)

Calibration ("relative effort, absolute budget"): per team and month
    capacity = headcount * hours_per_engineer_month * allocatable_fraction
    k        = min(capacity / sum(H_raw), calibration_clamp_max)
    H(pr)    = k * H_raw(pr)
Whatever capacity k cannot cover (clamp hit, or a quiet month) is booked as `unattributed`,
so features + overhead + unmapped + unattributed == capacity, always (the conservation invariant).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .types import Feature, OrgSettings, Team, WorkItem, WorkKind

TYPE_MULTIPLIER: dict[WorkKind, float] = {
    WorkKind.FEATURE: 1.0,
    WorkKind.CHORE: 0.8,
    WorkKind.BUG: 1.3,
    WorkKind.REVERT: 1.5,
    WorkKind.HOTFIX: 2.0,
    WorkKind.INCIDENT: 2.0,
}
P0_MULTIPLIER = 3.0
BUG_KINDS = {WorkKind.BUG, WorkKind.HOTFIX, WorkKind.INCIDENT, WorkKind.REVERT}


def type_multiplier(kind: WorkKind, priority: str | None) -> float:
    if kind in (WorkKind.HOTFIX, WorkKind.INCIDENT) and (priority or "").upper() == "P0":
        return P0_MULTIPLIER
    return TYPE_MULTIPLIER[kind]


def raw_effort_hours(
    additions: int,
    deletions: int,
    files_changed: int,
    review_rounds: int,
    kind: WorkKind,
    priority: str | None = None,
    incident_response_hours: float = 0.0,
) -> float:
    if min(additions, deletions, files_changed, review_rounds) < 0:
        raise ValueError("PR stats must be non-negative")
    churn = additions + 0.5 * deletions
    size = 0.5 + 1.2 * math.log1p(churn / 20.0)
    review = 1.0 + 0.15 * min(review_rounds, 6)
    spread = 1.0 + 0.05 * min(max(files_changed - 1, 0), 20)
    return size * review * spread * type_multiplier(kind, priority) + max(incident_response_hours, 0.0)


@dataclass
class TeamCalibration:
    team_id: str
    period_month: date
    capacity_hours: float
    raw_hours: float
    factor: float
    feature_hours: float = 0.0
    overhead_hours: float = 0.0
    unmapped_hours: float = 0.0

    @property
    def attributed_hours(self) -> float:
        return self.feature_hours + self.overhead_hours + self.unmapped_hours

    @property
    def unattributed_hours(self) -> float:
        return max(self.capacity_hours - self.attributed_hours, 0.0)


@dataclass
class FeatureEffort:
    hours: float = 0.0
    bug_hours: float = 0.0


def team_capacity_hours(team: Team, s: OrgSettings) -> float:
    return team.headcount * s.hours_per_engineer_month * s.allocatable_fraction


def allocate_effort(
    work_items: list[WorkItem],
    teams: list[Team],
    features: list[Feature],
    settings: OrgSettings,
    months: list[date],
) -> tuple[dict[tuple[date, str], FeatureEffort], dict[tuple[date, str], TeamCalibration]]:
    """Returns ((month, feature_id) -> effort, (month, team_id) -> calibration).

    Sweeping PRs (framework upgrades touching >50 features) and allocations to shared-overhead
    features are booked to overhead rather than smeared across product features.
    """
    overhead_ids = {f.id for f in features if f.is_shared_overhead}
    known_ids = {f.id for f in features}
    raw: dict[str, float] = {}
    team_raw: dict[tuple[date, str], float] = defaultdict(float)
    for wi in work_items:
        h = raw_effort_hours(wi.additions, wi.deletions, wi.files_changed, wi.review_rounds,
                             wi.kind, wi.priority, wi.incident_response_hours)
        raw[wi.id] = h
        team_raw[(wi.period_month, wi.team_id)] += h

    cal: dict[tuple[date, str], TeamCalibration] = {}
    for m in months:
        for t in teams:
            cap = team_capacity_hours(t, settings)
            r = team_raw.get((m, t.id), 0.0)
            k = min(cap / r, settings.calibration_clamp_max) if r > 0 else 0.0
            cal[(m, t.id)] = TeamCalibration(t.id, m, cap, r, k)

    out: dict[tuple[date, str], FeatureEffort] = defaultdict(FeatureEffort)
    for wi in work_items:
        c = cal.get((wi.period_month, wi.team_id))
        if c is None:  # work outside the computed window
            continue
        hours = raw[wi.id] * c.factor
        if wi.is_sweeping:
            c.overhead_hours += hours
            continue
        share_total = 0.0
        for fid, share in wi.allocations.items():
            if fid not in known_ids or share <= 0:
                continue
            share_total += share
            h = hours * share
            if fid in overhead_ids:
                c.overhead_hours += h
            else:
                c.feature_hours += h
            # overhead features also get an effort row, so the Platform line can be shown
            fe = out[(wi.period_month, fid)]
            fe.hours += h
            if wi.kind in BUG_KINDS:
                fe.bug_hours += h
        if share_total > 1.0 + 1e-6:
            raise ValueError(f"work item {wi.id} allocations sum to {share_total:.4f} > 1")
        c.unmapped_hours += hours * max(1.0 - share_total, 0.0)
    return dict(out), cal
