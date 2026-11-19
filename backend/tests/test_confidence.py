"""How sure is each number: the confidence scoring behind the ledger badge and "why this number"."""
from app.reports.confidence import ENG_MODEL_CEILING, assess

BASE = dict(c_eng=10_000, c_support=1_000, c_infra=500, c_ai_tokens=0, v_usage=20_000, v_tier=18_000,
            accounts_with_revenue=True, eng_weight=100, eng_declared=100, tickets=20, tickets_measured=20,
            tickets_declared=20, infra_total=500, infra_tagged=500, team_unmapped_share=0.02)


def test_declared_and_measured_data_is_high_but_never_perfect():
    c = assess(BASE)
    assert c["level"] == "high" and c["verdict_stable"]
    assert c["cost_quality"] < 1.0                                      # engineering hours are modelled
    assert abs(c["coverage"]["eng_declared_share"] - 1.0) < 1e-9
    eng_only = assess({**BASE, "c_support": 0, "c_infra": 0, "tickets": 0, "infra_total": 0})
    assert abs(eng_only["cost_quality"] - ENG_MODEL_CEILING) < 1e-9


def test_inferred_mappings_and_estimates_lower_confidence_with_reasons():
    c = assess({**BASE, "eng_declared": 30, "tickets_measured": 5, "infra_tagged": 100})
    assert c["level"] in ("medium", "low")
    text = " ".join(c["reasons"])
    assert "70% of engineering effort comes from inferred code mappings" in text
    assert "support time is estimated for 75% of its 20 ticket(s)" in text
    assert "80% of its infra/AI cost is spread from untagged spend" in text


def test_no_revenue_data_is_low_and_says_why():
    c = assess({**BASE, "v_usage": 0, "v_tier": 0, "accounts_with_revenue": False})
    assert c["level"] == "low" and not c["has_value_data"]
    assert any("value side is unknown" in r for r in c["reasons"])


def test_a_verdict_that_flips_between_models_is_low():
    c = assess({**BASE, "v_usage": 12_000, "v_tier": 9_000})          # cost 11,500: + under one model, - under the other
    assert c["level"] == "low" and not c["verdict_stable"]
    assert c["net_by_model"] == {"usage_weighted": 500.0, "tier_gated": -2500.0}
    assert any("flips between attribution models" in r for r in c["reasons"])


def test_shared_platform_cost_is_judged_on_cost_only():
    c = assess({**BASE, "is_shared_overhead": True, "v_usage": 0, "v_tier": 0, "accounts_with_revenue": False})
    assert c["level"] == "high" and any("no revenue side by design" in r for r in c["reasons"])


def test_a_team_with_much_unmapped_work_is_flagged():
    c = assess({**BASE, "team_unmapped_share": 0.3})
    assert any("30% of the owning team's hours map to no feature" in r for r in c["reasons"])
