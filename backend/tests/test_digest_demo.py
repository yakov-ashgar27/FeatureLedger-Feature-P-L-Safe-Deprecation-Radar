"""Phase 5: executive digest payloads and the live-demo seed re-keying."""
import asyncio
import json
import re
from pathlib import Path

import pytest

from app.demo import DEMO_NS, demo_id, demo_seed_sql
from app.reports import digest as dg
from app.seed.generate import build

SUMMARY = {
    "org": {"name": "Acme Cloud", "slug": "acme-demo", "demo": True}, "period": "2026-11", "ledger_period": "2026-10",
    "generated_at": "2026-11-19T08:00:00+00:00",
    "zombie_tax": {"outstanding": {"cost_month": 56_823.4, "eng_hours_month": 562.8, "fte": 5.02, "features": ["Bulk"]},
                   "reclaimed_this_month": {"cost_month": 20_661.0, "eng_hours_month": 160.0, "fte": 1.43,
                                            "annualized": 247_932.0, "sunsets": 1},
                   "reclaimed_to_date": {"cost_month": 20_661.0, "eng_hours_month": 160.0, "fte": 1.43,
                                         "annualized": 247_932.0, "sunsets": 1}},
    "sunsets_completed": [{"feature": "Zapier v1 Webhooks", "key": "zapier-v1-webhooks", "cost_month": 20_661.0,
                           "eng_hours_month": 160.0, "fte": 1.43, "archived_at": "2026-11-18T12:00:00+00:00"}],
    "sunsets_in_flight": [{"feature": "Bulk CSV Import v1", "key": "bulk-csv-import-v1", "state": "blast_radius_checked",
                           "days_in_state": 3, "rollbacks": 0}],
    "top_bleeders": [
        {"key": "ai-summaries", "name": "AI Summaries", "kind": "margin_bleed", "bleed_month": 5_300.0, "net_month": -900.0,
         "ai_tokens_month": 22_821.0, "gross_margin_pct": -0.0006, "adoption": 0.57, "reason": "Gross margin 0% <tokens>"},
        {"key": "legacy-pdf-export", "name": "Legacy PDF Exporter", "kind": "net_loss", "bleed_month": 22_864.0,
         "net_month": -22_864.0, "ai_tokens_month": 0, "gross_margin_pct": 0.9, "adoption": 0.025, "reason": "Costs more"}],
    "ai": {"token_spend_month": 22_821.24, "margin_bleed_features": ["AI Summaries"]},
    "data_health": {"status": "attention", "score": 0.83, "checks": [
        {"name": "Connectors", "ok": True, "detail": "5/6 healthy"},
        {"name": "Triage queue", "ok": False, "detail": "41 mapping(s) waiting for a decision"}]},
    "links": {"brief": "http://localhost:3000/reports?period=2026-11", "dashboard": "http://localhost:3000"},
}


def test_slack_block_kit_payload():
    p = dg.slack_blocks(SUMMARY)
    kinds = [b["type"] for b in p["blocks"]]
    assert kinds[0] == "header" and "actions" in kinds and "context" in kinds
    text = json.dumps(p)
    assert "$20.7k/mo" in text and "1.43 FTE" in text and "Zapier v1 Webhooks" in text
    assert "AI margin bleed" in text and "GM -0%" in text
    assert "Triage queue: 41 mapping(s)" in text                         # only failing checks are spelled out
    assert len(p["blocks"]) <= 50 and all(len(b.get("text", {}).get("text", "")) <= 3000 for b in p["blocks"])
    assert p["blocks"][-1]["elements"][0]["url"] == SUMMARY["links"]["brief"]


def test_brief_html_is_self_contained_and_escaped():
    h = dg.render_html(SUMMARY)
    assert h.startswith("<!DOCTYPE html>") and "@page" in h and "<script" not in h and "http" not in h.split("<body>")[0]
    assert "&lt;tokens&gt;" in h and "<tokens>" not in h
    assert "AI margin bleed" in h and "Zapier v1 Webhooks" in h and "live demo data" in h
    assert "no individual developer metrics" in h


def test_slack_never_posts_from_sandbox_or_demo(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/x")
    assert asyncio.run(dg.post_to_slack({"text": "x"}, demo=True)) == ("dry_run", None)
    assert dg.config.sandbox_mode           # the default; a sandbox never posts either
    assert asyncio.run(dg.post_to_slack({"text": "x"}, demo=False))[0] == "dry_run"


def test_period_helpers():
    from datetime import date
    assert dg.previous_month(date(2026, 1, 1)) == date(2025, 12, 1)
    assert dg.month_start(date(2026, 11, 19)) == date(2026, 11, 1)


@pytest.fixture(scope="module")
def seed():
    return build((Path(__file__).resolve().parents[2] / "features.yaml").read_text())


def test_demo_seed_is_a_separate_deterministic_tenant(seed):
    sql = demo_seed_sql(seed.sql, "acme-demo", "Acme Cloud")
    assert seed.org_id not in sql and demo_id(seed.org_id) in sql
    original = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", seed.sql))
    remapped = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sql))
    assert original and not (original & remapped)                     # no id is shared with the real seed tenant
    assert len(remapped) == len(original)                             # the mapping is one-to-one
    assert ",'Acme Cloud','acme-demo')" in sql and "@northwind.io" not in sql and "@acme-cloud.demo" in sql
    assert "'openai_usage'" not in sql and "BEGIN;" not in sql.splitlines()
    assert demo_seed_sql(seed.sql, "acme-demo", "Acme Cloud") == sql   # a reset recreates the same ids
    assert demo_id("x") == str(__import__("uuid").uuid5(DEMO_NS, "x"))
