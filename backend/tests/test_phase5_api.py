"""Phase 5 over HTTP against real Postgres: AI margin fields, Zombie-Guard via the GitHub webhook, the executive
digest, and the live demo (launch, role switching, reset, isolation from the real tenant).

    FL_INTEGRATION=1 SANDBOX_MODE=true DEMO_MODE=true DATABASE_URL=... ADMIN_DATABASE_URL=... \
        pytest tests/test_phase5_api.py
"""
from __future__ import annotations

import os

import pytest

if os.environ.get("FL_INTEGRATION") != "1":
    pytest.skip("integration suite: set FL_INTEGRATION=1 with a migrated, seeded database", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import config  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    assert config.demo_mode, "run with DEMO_MODE=true"
    with TestClient(app) as c:
        yield c


def H(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def login(client, role: str) -> str:
    t = client.post("/api/v1/auth/dev-login", json={"role": role}).json()["token"]
    client.cookies.clear()
    return t


def demo(client, role: str = "pm_tech_lead") -> str:
    r = client.post("/api/v1/demo/launch", json={"role": role})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()["token"]


# ------------------------------------------------------------------------------------- AI margin
def test_ledger_exposes_ai_gross_margin_and_margin_bleed(client):
    v = login(client, "viewer")
    led = client.get("/api/v1/pnl?window=1", headers=H(v)).json()
    ai = next(f for f in led["features"] if f["key"] == "ai-summaries")
    assert ai["margin_bleed"] is True and ai["c_ai_tokens"] > ai["c_infra"] > 0
    assert ai["gross_margin_pct"] < 0.20 and "free" in ai["ai_tier_breakdown"]
    assert led["summary"]["margin_bleed_features"] == ["ai-summaries"] and led["summary"]["ai_token_spend_month"] > 0
    radar = client.get("/api/v1/ai-margin", headers=H(v)).json()
    assert radar["features"][0]["key"] == "ai-summaries" and radar["features"][0]["margin_bleed"]
    sheet = client.get("/api/v1/features/ai-summaries", headers=H(v)).json()
    assert sheet["history"][-1]["margin_bleed"] is True


# ------------------------------------------------------------------------------------- Zombie-Guard
def test_zombie_guard_comments_once_and_updates_in_place(client):
    a = login(client, "admin")
    gh = next(i for i in client.get("/api/v1/integrations", headers=H(a)).json()["integrations"] if i["kind"] == "github")
    url = f"/api/v1/webhooks/github/{gh['id']}"
    hdr = {"X-GitHub-Event": "pull_request", "X-FeatureLedger-Secret": "sandbox"}

    def send(action, sha, files, number=4242, ref="fix/pdf-header"):
        body = {"action": action, "number": number, "repository": {"full_name": "northwind/app"},
                "pull_request": {"number": number, "head": {"ref": ref, "sha": sha}},
                "featureledger_sandbox_files": files}
        r = client.post(url, json=body, headers=hdr)
        assert r.status_code == 202, r.text
        return r.json()

    pdf = ["services/export/pdf_v1/render.py", "services/export/pdf_v1/api.py"]
    first = send("opened", "a" * 40, pdf)
    assert first["zombie_guard"] == "created" and first["features"] == ["legacy-pdf-export"]
    assert send("synchronize", "b" * 40, pdf)["zombie_guard"] == "unchanged"
    more = send("synchronize", "c" * 40, pdf + ["services/integrations/zapier_v1/hooks.py"])
    assert more["zombie_guard"] == "updated" and more["comment_id"] == first["comment_id"]
    assert send("opened", "d" * 40, ["services/editor/core.py"], number=4243)["zombie_guard"] == "clean"
    assert send("opened", "e" * 40, pdf, number=4244, ref="sunset/legacy-pdf-export")["zombie_guard"] == "ignored"
    rows = [c for c in client.get("/api/v1/zombie-guard/comments", headers=H(a)).json() if c["pr_number"] == 4242]
    assert len(rows) == 1 and rows[0]["updates"] == 1 and "Zombie-Guard" in rows[0]["body"]
    assert "Net P&L (3 mo)" in rows[0]["body"] and "Legacy PDF Exporter" in rows[0]["body"]
    pv = client.post("/api/v1/zombie-guard/preview", json={"paths": pdf}, headers=H(login(client, "viewer"))).json()
    assert pv["flagged"] and pv["comment_markdown"].startswith("<!-- featureledger:zombie-guard -->")


# ------------------------------------------------------------------------------------- digest
def test_executive_digest_preview_send_and_brief(client):
    v, p = login(client, "viewer"), login(client, "pm_tech_lead")
    prev = client.get("/api/v1/reports/executive-digest", headers=H(v)).json()
    s = prev["summary"]
    assert s["zombie_tax"]["outstanding"]["fte"] > 0 and len(s["top_bleeders"]) == 3
    assert any(b["kind"] == "margin_bleed" and b["key"] == "ai-summaries" for b in s["top_bleeders"])
    assert s["data_health"]["status"] in ("healthy", "attention", "poor") and prev["slack"]["blocks"][0]["type"] == "header"
    assert client.post("/api/v1/reports/executive-digest", json={}, headers=H(v)).status_code == 403
    r = client.post("/api/v1/reports/executive-digest", json={"post_to_slack": True}, headers=H(p))
    assert r.status_code == 201 and r.json()["slack_status"] == "dry_run"
    hist = client.get("/api/v1/reports/executive-digests", headers=H(v)).json()
    assert hist[0]["id"] == r.json()["id"] and hist[0]["trigger"] == "manual"
    html = client.get("/api/v1/reports/executive-brief.html", headers=H(v))
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    assert "Executive Brief" in html.text and "Top bleeding features" in html.text


# ------------------------------------------------------------------------------------- live demo
def test_demo_launch_switch_and_isolation(client):
    st = client.get("/api/v1/demo/status").json()
    assert st["enabled"] and st["ready"] and st["engineers"] == 45 and "Acme Cloud" in st["label"]
    t = demo(client)
    me = client.get("/api/v1/auth/me", headers=H(t)).json()
    assert me["demo"] is True and me["role"] == "pm_tech_lead" and me["org"]["slug"] == config.demo_org_slug
    real = login(client, "admin")
    real_me = client.get("/api/v1/auth/me", headers=H(real)).json()
    assert real_me["demo"] is False and real_me["org"]["id"] != me["org"]["id"]

    # role switching: only inside the demo
    sw = client.post("/api/v1/demo/switch-role", json={"role": "admin"}, headers=H(t))
    assert sw.status_code == 200 and sw.json()["user"]["role"] == "admin"
    admin_demo = sw.json()["token"]
    assert client.post("/api/v1/demo/switch-role", json={"role": "viewer"}, headers=H(real)).status_code == 403
    assert client.post("/api/v1/demo/reset", headers=H(login(client, "viewer"))).status_code == 403

    # isolation: the demo sees its own plans with its own ids, never the real tenant's
    demo_plans = client.get("/api/v1/sunset-plans", headers=H(t)).json()
    real_plans = client.get("/api/v1/sunset-plans", headers=H(real)).json()
    assert demo_plans and not ({p["id"] for p in demo_plans} & {p["id"] for p in real_plans})
    assert client.get(f"/api/v1/sunset-plans/{real_plans[0]['id']}", headers=H(t)).status_code == 404
    assert client.get(f"/api/v1/sunset-plans/{demo_plans[0]['id']}", headers=H(real)).status_code == 404
    assert client.get("/api/v1/users", headers=H(admin_demo)).json()[0]["email"].endswith("@acme-cloud.demo")
    uid = client.get("/api/v1/users", headers=H(admin_demo)).json()[0]["id"]
    assert client.patch(f"/api/v1/users/{uid}", json={"role": "viewer"}, headers=H(admin_demo)).status_code == 409

    # the demo's safe-sunset machine works end to end with sandbox providers only
    zap = next(p for p in demo_plans if p["feature_key"] == "zapier-v1-webhooks")
    r = client.post(f"/api/v1/sunset-plans/{zap['id']}/advance", json={}, headers=H(admin_demo))
    assert r.status_code == 200, r.text
    assert r.json()["cleanup_pr"]["provider"] == "sandbox" and r.json()["cleanup_pr"]["repo"] == "acme/app"
    created = client.post("/api/v1/sunset-plans", json={"feature_key": "legacy-pdf-export", "reason": "demo visitor test"},
                          headers=H(t))
    assert created.status_code == 201

    # reset: back to the seed state, same ids, sessions still valid
    real_ledger = client.get("/api/v1/pnl?window=3", headers=H(real)).json()
    r = client.post("/api/v1/demo/reset", headers=H(t))
    assert r.status_code == 200, r.text
    after = client.get("/api/v1/sunset-plans", headers=H(t)).json()
    assert {p["id"] for p in after} == {p["id"] for p in demo_plans}
    assert next(p for p in after if p["feature_key"] == "zapier-v1-webhooks")["state"] == "canary_dark_launch"
    assert all(p["feature_key"] != "legacy-pdf-export" for p in after)
    led = client.get("/api/v1/pnl?window=1", headers=H(t)).json()
    assert led["summary"]["zombie_count"] == 3 and led["summary"]["margin_bleed_features"] == ["ai-summaries"]
    # zero drift: a reset leaves the demo at its steady state, so another full sandbox sync (connectors,
    # inbox, Tier 2 + Tier 3, recompute) changes nothing at all, not even by a cent
    from app.demo import converge
    full = "/api/v1/pnl?window=6&include_archived=true"
    before = client.get(full, headers=H(t)).json()
    sheets_before = {k: client.get(f"/api/v1/features/{k}", headers=H(t)).json()["history"]
                     for k in ("ai-summaries", "legacy-pdf-export", "document-editor")}
    resync = client.portal.call(converge, me["org"]["id"])
    assert resync["processing"]["processed"] > 0 or resync["runs"]          # it really ran
    after = client.get(full, headers=H(t)).json()
    assert after == before
    assert after["summary"]["monthly_cost"] - before["summary"]["monthly_cost"] == 0
    for k, hist in sheets_before.items():
        assert client.get(f"/api/v1/features/{k}", headers=H(t)).json()["history"] == hist

    # the real tenant was untouched by the reset (regression: an admin-connection recompute once wiped every ledger)
    assert client.get("/api/v1/sunset-plans", headers=H(real)).json() == real_plans
    assert client.get("/api/v1/pnl?window=3", headers=H(real)).json() == real_ledger
