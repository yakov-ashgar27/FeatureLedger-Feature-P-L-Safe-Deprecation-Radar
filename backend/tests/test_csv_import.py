import pytest

from app.core.manifest import parse_manifest
from app.importers.csv_import import ImportError_, parse_csv

M = parse_manifest("""
version: 1
features:
  - {key: legacy-pdf-export, name: Legacy PDF, events: [pdf_exported], support_tags: [pdf-export]}
  - {key: editor, name: Editor}
""")


def test_accounts_ok_with_bom_and_money_format():
    p = parse_csv("accounts", "﻿external_ref,name,plan_tier,arr_usd,renewal_date,health,csm_email\n"
                              'cus_1,Globex,Enterprise,"$180,000",2026-12-15,green,dana@x.io\n', M)
    assert p.rows[0]["arr_usd"] == 180000 and p.rows[0]["plan_tier"] == "enterprise"


def test_usage_resolves_events_and_rejects_unknown():
    p = parse_csv("usage", "account_ref,feature,active_days,active_users,events\n"
                           "cus_1,pdf_exported,3,2,40\ncus_1,editor,20,30,5000\n", M)
    assert [r["feature_key"] for r in p.rows] == ["legacy-pdf-export", "editor"]
    with pytest.raises(ImportError_) as e:
        parse_csv("usage", "account_ref,feature,active_days,active_users\ncus_1,nope,3,2\ncus_1,editor,40,1\n", M)
    assert any("line 2" in x for x in e.value.errors) and any("line 3" in x and "active_days" in x for x in e.value.errors)


def test_tickets_support_tag_and_missing_columns():
    p = parse_csv("tickets", "external_id,account_ref,feature,created_at,handle_minutes\n"
                             "ZD-1,cus_1,pdf-export,2026-10-03T10:00:00Z,45\n", M)
    assert p.rows[0]["feature_key"] == "legacy-pdf-export"
    with pytest.raises(ImportError_) as e:
        parse_csv("tickets", "id,feature\nx,y\n", M)
    assert "missing column" in e.value.errors[0]


def test_costs_allow_untagged_and_duplicates():
    p = parse_csv("costs", "feature,cost_type,amount_usd\neditor,infra,100\neditor,infra,50\n,infra,3000\n", M)
    assert len(p.rows) == 3 and p.rows[2]["feature_key"] is None


def test_duplicate_accounts_rejected():
    with pytest.raises(ImportError_):
        parse_csv("accounts", "external_ref,name,plan_tier,arr_usd\nc1,A,pro,1\nc1,B,pro,2\n", M)
