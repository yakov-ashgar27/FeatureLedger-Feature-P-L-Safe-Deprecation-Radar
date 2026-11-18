from datetime import date

import pytest

from app.core.effort import allocate_effort, raw_effort_hours
from app.core.types import Feature, OrgSettings, Team, WorkItem, WorkKind

M = date(2026, 10, 1)


def test_typo_vs_p0_hotfix_are_far_apart():
    typo = raw_effort_hours(2, 0, 1, 0, WorkKind.CHORE)
    bug = raw_effort_hours(120, 0, 4, 1, WorkKind.BUG)
    p0 = raw_effort_hours(300, 0, 6, 2, WorkKind.HOTFIX, "P0", incident_response_hours=12)
    assert typo == pytest.approx(0.4915, abs=1e-3)
    assert bug == pytest.approx(4.8742, abs=1e-3)
    assert p0 == pytest.approx(30.6571, abs=1e-3)
    assert p0 / typo > 50


def test_deletions_count_half():
    assert raw_effort_hours(0, 200, 1, 0, WorkKind.FEATURE) == pytest.approx(
        raw_effort_hours(100, 0, 1, 0, WorkKind.FEATURE))


def test_negative_stats_rejected():
    with pytest.raises(ValueError):
        raw_effort_hours(-1, 0, 1, 0, WorkKind.BUG)


def _wi(i, adds, alloc, kind=WorkKind.FEATURE, sweeping=False, team="t1"):
    return WorkItem(id=f"w{i}", team_id=team, period_month=M, kind=kind, priority=None, additions=adds,
                    deletions=0, files_changed=1, review_rounds=0, allocations=alloc, is_sweeping=sweeping)


def test_calibration_conserves_capacity():
    s = OrgSettings()
    teams = [Team("t1", "core", "Core", 6)]
    feats = [Feature("a", "a", "A"), Feature("b", "b", "B"),
             Feature("o", "platform", "Platform", is_shared_overhead=True)]
    items = [_wi(1, 400, {"a": 1.0}), _wi(2, 400, {"a": 0.5, "b": 0.3}), _wi(3, 100, {"o": 1.0}),
             _wi(4, 900, {}, sweeping=True), _wi(5, 50, {"b": 1.0}, kind=WorkKind.BUG)]
    effort, cal = allocate_effort(items, teams, feats, s, [M])
    c = cal[(M, "t1")]
    capacity = 6 * 140 * 0.8
    assert c.capacity_hours == pytest.approx(capacity)
    feature_hours = effort[(M, "a")].hours + effort[(M, "b")].hours
    assert c.feature_hours == pytest.approx(feature_hours)
    total = c.feature_hours + c.overhead_hours + c.unmapped_hours + c.unattributed_hours
    assert total == pytest.approx(capacity, rel=1e-9)
    assert effort[(M, "b")].bug_hours > 0
    assert effort[(M, "o")].hours == pytest.approx(c.overhead_hours - raw_effort_hours(
        900, 0, 1, 0, WorkKind.FEATURE) * c.factor)


def test_clamp_books_remainder_as_unattributed():
    s = OrgSettings()
    teams = [Team("t1", "core", "Core", 5)]
    feats = [Feature("a", "a", "A")]
    effort, cal = allocate_effort([_wi(1, 10, {"a": 1.0})], teams, feats, s, [M])
    c = cal[(M, "t1")]
    assert c.factor == s.calibration_clamp_max
    assert c.unattributed_hours == pytest.approx(c.capacity_hours - effort[(M, "a")].hours)


def test_allocations_over_one_rejected():
    with pytest.raises(ValueError):
        allocate_effort([_wi(1, 10, {"a": 0.8, "b": 0.5})], [Team("t1", "c", "C", 3)],
                        [Feature("a", "a", "A"), Feature("b", "b", "B")], OrgSettings(), [M])
