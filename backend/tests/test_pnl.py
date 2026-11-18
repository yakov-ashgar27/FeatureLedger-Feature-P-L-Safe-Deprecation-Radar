from datetime import date

import pytest

from app.core.pnl import FeaturePnl, classify, compute_pnl
from app.core.types import (Account, AttributionModel, CostLine, Dataset, Feature, FeatureClass,
                            OrgSettings, Team, Ticket, Usage, WorkItem, WorkKind)

S = OrgSettings()
MONTHS = [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]


def row(m, **kw):
    r = FeaturePnl("f", "f", m)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_worked_example_legacy_pdf():
    """docs/SPEC.md §4.6: 61 calibrated hours, 38 tickets x 45 min, $410 infra, $810 attributed."""
    r = row(MONTHS[-1], eng_hours=61, c_eng=61 * 95, c_support=38 * 0.75 * 45, c_infra=410, v_usage=810)
    assert r.c_total == pytest.approx(7_487.50)
    assert r.net_roi() == pytest.approx(-6_677.50)
    assert r.efficiency() == pytest.approx(0.1082, abs=1e-4)


def test_value_models():
    r = row(MONTHS[-1], v_usage=100, v_tier=150, revenue_at_risk_arr=120_000)
    assert r.value(AttributionModel.USAGE_WEIGHTED) == 100
    assert r.value(AttributionModel.TIER_GATED) == 150
    assert r.value(AttributionModel.CRITICAL_DEPENDENCY) == 10_000


def test_classification_order():
    f = Feature("f", "f", "F", launched_at=date(2021, 1, 1))
    zombie = [row(m, c_eng=3000, eng_hours=30, bug_hours=15, v_usage=200, active_accounts=4,
                  eligible_accounts=200) for m in MONTHS]
    assert classify(f, zombie, S)[0] is FeatureClass.ZOMBIE
    anchor = [row(m, c_eng=3000, eng_hours=30, bug_hours=15, v_usage=200, active_accounts=4,
                  eligible_accounts=200, anchor_accounts=1, revenue_at_risk_arr=126_000) for m in MONTHS]
    assert classify(f, anchor, S)[0] is FeatureClass.NICHE_ANCHOR  # anchor wins over zombie
    reg = Feature("g", "gdpr", "GDPR", is_regulatory=True)
    assert classify(reg, zombie, S)[0] is FeatureClass.MANDATED
    plat = Feature("p", "auth", "Auth", is_shared_overhead=True)
    assert classify(plat, zombie, S)[0] is FeatureClass.PLATFORM
    young = Feature("ai", "ai", "AI", launched_at=date(2026, 5, 1))
    growth = [row(m, c_eng=9000, c_infra=6000, v_usage=4000, active_accounts=20 + 10 * i,
                  eligible_accounts=200) for i, m in enumerate(MONTHS)]
    assert classify(young, growth, S)[0] is FeatureClass.GROWTH_BET
    cow = [row(m, c_eng=10_000, v_usage=60_000, active_accounts=180, eligible_accounts=200) for m in MONTHS]
    assert classify(f, cow, S)[0] is FeatureClass.CASH_COW
    # a rising feature is never a zombie even if currently unprofitable
    rising = [row(m, c_eng=3000, eng_hours=30, bug_hours=15, v_usage=200, active_accounts=2 + i,
                  eligible_accounts=200) for i, m in enumerate(MONTHS)]
    assert classify(f, rising, S)[0] is not FeatureClass.ZOMBIE


def test_compute_pnl_end_to_end_small():
    t = Team("t", "core", "Core", 3)
    feats = [Feature("ed", "editor", "Editor", owner_team_id="t", launched_at=date(2020, 1, 1)),
             Feature("pdf", "pdf", "PDF", owner_team_id="t", launched_at=date(2020, 1, 1)),
             Feature("auth", "auth", "Auth", is_shared_overhead=True, allocation_rule="proportional_usage")]
    m = MONTHS[-1]
    wis = [WorkItem("w1", "t", m, WorkKind.FEATURE, None, 400, 50, 5, 1, allocations={"ed": 1.0}),
           WorkItem("w2", "t", m, WorkKind.BUG, "P2", 80, 20, 2, 1, allocations={"pdf": 1.0}),
           WorkItem("w3", "t", m, WorkKind.CHORE, None, 60, 0, 1, 0, allocations={"auth": 1.0})]
    accts = [Account("a", "A", "pro", 120_000, None, "green")]
    usage = [Usage("a", "ed", m, 1000, 20, 10), Usage("a", "pdf", m, 3, 1, 1)]
    ds = Dataset(S, [t], feats, wis, {m: accts}, usage, {(m, "a"): 20},
                 [Ticket("k1", "pdf", "a", m, 90)], [CostLine("pdf", m, "infra", 100), CostLine(None, m, "infra", 50)])
    res = compute_pnl(ds, [m])
    ed, pdf, auth = res.rows[(m, "ed")], res.rows[(m, "pdf")], res.rows[(m, "auth")]
    cap = 3 * 140 * 0.8
    c = res.calibration[(m, "t")]
    assert c.factor == S.calibration_clamp_max  # 3 small PRs can't explain a 3-person month
    assert ed.eng_hours + pdf.eng_hours + auth.eng_hours + c.unattributed_hours == pytest.approx(cap)
    assert pdf.c_support == pytest.approx(1.5 * 45)
    assert pdf.c_infra == 100 and res.untagged_infra[m] == 50
    assert ed.v_usage + pdf.v_usage == pytest.approx(10_000)
    # overhead allocated in proportion to usage value, fully
    assert ed.c_overhead_alloc + pdf.c_overhead_alloc == pytest.approx(auth.c_total)
    assert auth.classification is FeatureClass.PLATFORM
    assert pdf.bug_tax_ratio == pytest.approx(1.0)


def test_no_usage_data_never_yields_zombie():
    f = Feature("f", "f", "F", launched_at=date(2021, 1, 1))
    git_only = [row(m, c_eng=9000, eng_hours=90, bug_hours=60, eligible_accounts=0) for m in MONTHS]
    cls, why = classify(f, git_only, S)
    assert cls is FeatureClass.WATCH and "import" in why
