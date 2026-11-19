"""Phase 5: AI gross margin and margin_bleed detection."""
from datetime import date
from pathlib import Path

import pytest

from app.core.pnl import FeaturePnl, compute_pnl, detect_margin_bleed
from app.core.types import Feature, OrgSettings
from app.seed.generate import build

S = OrgSettings()


def feat(**kw):
    base = dict(id="f1", key="ai", name="AI", owner_team_id="t1")
    base.update(kw)
    return Feature(**base)


def row(adoption=0.4, v=10_000.0, ai=8_000.0, infra=1_000.0, tiers=None):
    r = FeaturePnl("f1", "ai", date(2026, 10, 1), c_infra=infra, c_ai_tokens=ai, v_usage=v,
                   active_accounts=int(adoption * 100), eligible_accounts=100)
    r.ai_tier_breakdown = tiers or {}
    return r


def test_gross_margin_formula_and_cost_conservation():
    r = row(v=10_000, ai=3_000, infra=1_000)
    r.c_eng, r.c_support = 5_000, 500
    assert r.gross_margin_pct == pytest.approx(0.6)
    assert r.c_total == pytest.approx(5_000 + 500 + 1_000 + 3_000)
    assert row(v=0, ai=500, infra=0).gross_margin_pct == pytest.approx(-500)   # divided by max(v, 1)


def test_margin_bleed_rule():
    flagged, why = detect_margin_bleed(feat(), row(adoption=0.40, v=10_000, ai=8_000, infra=1_000), S)
    assert flagged and "Gross margin 10%" in why and "40% adoption" in why
    assert not detect_margin_bleed(feat(), row(adoption=0.10), S)[0]                 # not popular enough
    assert not detect_margin_bleed(feat(), row(ai=3_000, infra=1_000), S)[0]         # 60% margin is fine
    assert not detect_margin_bleed(feat(), row(ai=2_000, infra=7_500), S)[0]         # infra-driven, not tokens
    assert not detect_margin_bleed(feat(), row(ai=0, infra=9_500), S)[0]             # no AI spend at all
    assert not detect_margin_bleed(feat(is_shared_overhead=True), row(), S)[0]
    assert detect_margin_bleed(feat(), row(v=5_000, ai=6_000, infra=500), S)[0]      # negative margin
    strict = OrgSettings(margin_bleed_max_gross_margin=0.05)
    assert not detect_margin_bleed(feat(), row(v=10_000, ai=8_000, infra=1_000), strict)[0]


def test_margin_bleed_reason_calls_out_free_and_low_tiers():
    tiers = {"free": {"tokens_usd": 3_000, "revenue_usd": 0, "accounts": 40, "events": 1, "token_share": .375},
             "starter": {"tokens_usd": 1_000, "revenue_usd": 900, "accounts": 20, "events": 1, "token_share": .125},
             "enterprise": {"tokens_usd": 4_000, "revenue_usd": 9_100, "accounts": 5, "events": 1, "token_share": .5}}
    _, why = detect_margin_bleed(feat(), row(tiers=tiers), S)
    assert "free/starter accounts burn 50% of tokens ($4,000) for $900 revenue" in why


def test_seed_ai_summaries_bleeds_once_it_is_popular():
    seed = build((Path(__file__).resolve().parents[2] / "features.yaml").read_text())
    months = sorted(seed.dataset.accounts_by_month)
    res = compute_pnl(seed.dataset, months)
    ai = {m: next(r for r in res.month_rows(m) if r.feature_key == "ai-summaries") for m in months}
    first, last = ai[months[0]], ai[months[-1]]
    assert last.margin_bleed and last.adoption > 0.15 and last.gross_margin_pct < 0.20
    assert not first.margin_bleed                    # early: margin poor but adoption below 15%
    assert last.c_ai_tokens > last.c_infra > 0
    assert set(last.ai_tier_breakdown) >= {"free", "pro"}
    assert last.ai_tier_breakdown["free"]["revenue_usd"] == 0 and last.ai_tier_breakdown["free"]["tokens_usd"] > 0
    assert sum(t["tokens_usd"] for t in last.ai_tier_breakdown.values()) == pytest.approx(last.c_ai_tokens, abs=0.1)
    # the margin flag never changes the health class or the zombie list
    zombies = sorted(r.feature_key for r in res.month_rows(months[-1]) if r.classification.value == "zombie")
    assert zombies == ["bulk-csv-import-v1", "legacy-pdf-export", "zapier-v1-webhooks"]
    assert [r.feature_key for r in res.month_rows(months[-1]) if r.margin_bleed] == ["ai-summaries"]
