"""End-to-end: seed company -> full P&L engine -> the classifications the demo promises."""
from pathlib import Path

import pytest

from app.core.pnl import compute_pnl
from app.core.types import FeatureClass
from app.seed.generate import MONTHS, build

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def result():
    seed = build((ROOT / "features.yaml").read_text())
    return seed, compute_pnl(seed.dataset, MONTHS)


def by_key(res, m):
    return {r.feature_key: r for r in res.month_rows(m)}


def test_seed_is_deterministic():
    text = (ROOT / "features.yaml").read_text()
    assert build(text).sql == build(text).sql


def test_company_shape(result):
    seed, _ = result
    ds = seed.dataset
    assert sum(t.headcount for t in ds.teams) == 45
    arr = sum(a.arr_usd for a in ds.accounts_by_month[MONTHS[-1]])
    assert 12e6 < arr < 17e6


def test_expected_classifications(result):
    _, res = result
    rows = by_key(res, MONTHS[-1])
    expected = {
        "legacy-pdf-export": FeatureClass.ZOMBIE,
        "zapier-v1-webhooks": FeatureClass.ZOMBIE,
        "bulk-csv-import-v1": FeatureClass.ZOMBIE,
        "saml-attribute-mapping": FeatureClass.NICHE_ANCHOR,
        "gdpr-data-export": FeatureClass.MANDATED,
        "ai-summaries": FeatureClass.GROWTH_BET,
        "document-editor": FeatureClass.CASH_COW,
        "realtime-collab": FeatureClass.CASH_COW,
        "platform-auth-sso": FeatureClass.PLATFORM,
    }
    got = {k: rows[k].classification for k in expected}
    assert got == expected


def test_conservation_every_team_every_month(result):
    _, res = result
    for (m, _team), c in res.calibration.items():
        total = c.feature_hours + c.overhead_hours + c.unmapped_hours + c.unattributed_hours
        assert total == pytest.approx(c.capacity_hours, rel=1e-9)


def test_usage_value_sums_to_attributed_mrr(result):
    _, res = result
    for m in MONTHS:
        att = res.attribution[m]
        assert sum(r.v_usage for r in res.month_rows(m)) == pytest.approx(att.total_mrr - att.unattributed_mrr)
        assert sum(r.v_tier for r in res.month_rows(m)) == pytest.approx(att.total_mrr - att.unattributed_mrr)


def test_overhead_fully_allocated(result):
    _, res = result
    m = MONTHS[-1]
    rows = res.month_rows(m)
    pool = sum(r.c_total for r in rows if r.classification is FeatureClass.PLATFORM)
    assert sum(r.c_overhead_alloc for r in rows) == pytest.approx(pool)
