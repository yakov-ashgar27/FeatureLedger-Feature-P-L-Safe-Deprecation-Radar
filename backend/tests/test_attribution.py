import math
from datetime import date

import pytest

from app.core.attribution import attribute_month, churn_risk, engagement, renewal_proximity
from app.core.types import Account, Feature, OrgSettings, Usage

M = date(2026, 10, 1)
FEATS = [Feature("ed", "editor", "Editor"), Feature("pdf", "pdf", "PDF"),
         Feature("appr", "approvals", "Approvals", tier_gate="enterprise"),
         Feature("auth", "auth", "Auth", is_shared_overhead=True)]


def test_engagement_damps_users():
    assert engagement(3, 2) == pytest.approx(3 * math.log(3))
    assert engagement(0, 5) == 0
    assert engagement(5, 0) == 0


def test_usage_weighted_sums_to_mrr_of_active_accounts():
    accts = [Account("a", "A", "enterprise", 150_000, date(2027, 6, 1), "green"),
             Account("b", "B", "pro", 36_000, None, "green"),
             Account("c", "C", "pro", 12_000, None, "green")]  # no usage -> unattributed
    usage = [Usage("a", "ed", M, 900, 20, 40), Usage("a", "pdf", M, 4, 3, 2), Usage("a", "appr", M, 50, 10, 5),
             Usage("a", "auth", M, 9999, 20, 40),  # overhead usage is ignored for attribution
             Usage("b", "ed", M, 100, 15, 6)]
    res = attribute_month(M, accts, usage, {(M, "a"): 20, (M, "b"): 15}, FEATS, OrgSettings())
    total_usage = sum(res.v_usage.values())
    total_tier = sum(res.v_tier.values())
    assert total_usage == pytest.approx((150_000 + 36_000) / 12)
    assert total_tier == pytest.approx(total_usage)  # tier model redistributes, never creates revenue
    assert res.unattributed_mrr == pytest.approx(1_000)
    assert "auth" not in res.v_usage
    # account B only used the editor -> its whole MRR goes there
    e_a = {"ed": engagement(20, 40), "pdf": engagement(3, 2), "appr": engagement(10, 5)}
    expected_pdf = 12_500 * e_a["pdf"] / sum(e_a.values())
    assert res.v_usage["pdf"] == pytest.approx(expected_pdf)
    # enterprise uplift (35%) goes entirely to the only gated feature it used
    expected_appr_tier = 0.65 * 12_500 * e_a["appr"] / sum(e_a.values()) + 0.35 * 12_500
    assert res.v_tier["appr"] == pytest.approx(expected_appr_tier)
    assert res.paying_accounts == 3
    assert res.active_accounts["ed"] == 2


def test_churn_risk_components():
    assert renewal_proximity(date(2026, 12, 15), M) == 1.0
    assert renewal_proximity(date(2027, 3, 1), M) == 0.8
    assert renewal_proximity(None, M) == 0.6
    assert churn_risk(0.05, None, "red", M, 0.4) == 0.0
    # c=0.636 >= 0.40 -> saturates at 1, renewal in 75 days, healthy account
    assert churn_risk(14 / 22, date(2026, 12, 15), "green", M, 0.4) == pytest.approx(0.7)
    assert churn_risk(0.2, date(2027, 9, 1), "yellow", M, 0.4) == pytest.approx(0.5 * 0.6 * 0.9)


def test_anchor_detection_and_revenue_at_risk():
    accts = [Account("g", "Globex", "enterprise", 180_000, date(2026, 12, 15), "green")]
    usage = [Usage("g", "ed", M, 500, 22, 30), Usage("g", "pdf", M, 80, 14, 3)]
    res = attribute_month(M, accts, usage, {(M, "g"): 22}, FEATS, OrgSettings())
    assert res.anchor_accounts["pdf"] == 1
    assert res.revenue_at_risk["pdf"] == pytest.approx(180_000 * 0.7)
    assert res.revenue_at_risk["ed"] == pytest.approx(180_000 * 0.7)  # non-additive by design
