"""Phase 5 walkthrough against a running stack (docker compose up, DEMO_MODE=true), through the dashboard origin.

    python3 scripts/e2e_phase5.py [http://localhost:3000]

Live demo: launch without login, switch roles, run the safe-sunset machine, Trigger Sync (sandbox connectors
must rebuild the same numbers, not add to them), reset. Plus the Token-Bleed Radar, the Zombie-Guard preview,
and the executive digest / brief. Standard library only.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, __import__("os").path.dirname(__file__))
from e2e_phase4 import call, code, expect  # noqa: E402  (shared helpers; importing it does not run the walkthrough)

# ---------------------------------------------------------------- live demo: zero-login launch
s, st, _ = call("GET", "/api/v1/demo/status")
expect(s == 200 and st["enabled"] and st["ready"], f"demo status: {st.get('label')}")
expect(st["label"] == "Launch Live Demo (Acme Cloud — 45 Engineers)", "the landing button reads 'Launch Live Demo (Acme Cloud — 45 Engineers)'")
s, d, hdr = call("POST", "/api/v1/demo/launch", {})
expect(s == 200 and "fl_session=" in (hdr.get("Set-Cookie") or "") and d["user"]["role"] == "pm_tech_lead",
       "one click: signed into the demo org as PM / Tech lead (cookie set through the proxy)")
PM = d["token"]
s, html, _ = call("GET", "/", cookie=PM)
expect(s == 200 and b"LIVE DEMO" in html and b"Token-Bleed Radar" in html, "dashboard shows the demo banner and the Token-Bleed Radar")
for path in ("/simulator", "/sunset", "/zombie-guard", "/reports", "/features/ai-summaries", "/integrations"):
    s, _, _ = call("GET", path, cookie=PM)
    expect(s == 200, f"demo page {path} renders")

s, d, _ = call("POST", "/api/v1/demo/switch-role", {"role": "admin"}, cookie=PM)
expect(s == 200 and d["user"]["role"] == "admin", "role switch from the banner: now Admin")
ADM = d["token"]
s, real, _ = call("POST", "/api/v1/auth/dev-login", {"role": "admin"})
REAL = real["token"]
s, mine, _ = call("GET", "/api/v1/sunset-plans", cookie=ADM)
s, theirs, _ = call("GET", "/api/v1/sunset-plans", cookie=REAL)
expect(not ({p["id"] for p in mine} & {p["id"] for p in theirs}), "demo plans and real-tenant plans never overlap")
s, _, _ = call("GET", f"/api/v1/sunset-plans/{theirs[0]['id']}", cookie=ADM)
expect(s == 404, "a demo session cannot open a real tenant's plan (RLS)")
s, d, _ = call("POST", "/api/v1/demo/reset", cookie=REAL)
expect(s == 403, "a real-tenant session cannot reset anything through the demo endpoint")

# ---------------------------------------------------------------- reset, then Trigger Sync must not change the numbers
s, d, _ = call("POST", "/api/v1/demo/reset", cookie=ADM)
expect(s == 200, f"demo reset in {d.get('seconds')} s")
s, before, _ = call("GET", "/api/v1/pnl?window=1", cookie=ADM)
b = before["summary"]
s, ints, _ = call("GET", "/api/v1/integrations", cookie=ADM)
kinds = [i for i in ints["integrations"] if i["kind"] != "github"]
deadline = time.time() + 420


def last_run(iid: str):
    _, cur, _ = call("GET", "/api/v1/integrations", cookie=ADM)
    return next(iter(next(i for i in cur["integrations"] if i["id"] == iid)["recent_runs"]), None)


for i in kinds:      # one at a time, as a person clicking "Trigger Sync" would
    started, ok = datetime.now(timezone.utc), False
    while time.time() < deadline and not ok:
        s, q, _ = call("POST", f"/api/v1/integrations/{i['id']}/sync", cookie=ADM)
        expect(s in (200, 202), f"Trigger Sync {i['kind']} accepted")
        for _ in range(30):
            time.sleep(3)
            r = last_run(i["id"])
            if r and r["status"] != "running" and datetime.fromisoformat(r["started_at"]) >= started:
                ok = True
                break
            if not q.get("queued") and not (r and r["status"] == "running"):
                break        # another org-wide sync held the slot: ask again
    expect(ok, f"{i['kind']} sandbox sync finished")
same = False
while time.time() < deadline + 60 and not same:
    s, after, _ = call("GET", "/api/v1/pnl?window=1", cookie=ADM)
    a = after["summary"]
    same = (abs(a["monthly_cost"] - b["monthly_cost"]) <= 0.01 * b["monthly_cost"]
            and a["zombie_count"] == b["zombie_count"] and a["margin_bleed_features"] == b["margin_bleed_features"])
    if not same:
        time.sleep(5)
expect(same, f"after the syncs the demo ledger matches the seed (cost {b['monthly_cost']:.0f} -> {a['monthly_cost']:.0f}, "
             f"zombies {a['zombie_count']}, margin bleed {a['margin_bleed_features']})")

# ---------------------------------------------------------------- Token-Bleed Radar, Zombie-Guard, digest
s, radar, _ = call("GET", "/api/v1/ai-margin", cookie=PM)
top = radar["features"][0]
expect(top["key"] == "ai-summaries" and top["margin_bleed"], f"Token-Bleed Radar: {top['margin_bleed_reason']}")
s, pv, _ = call("POST", "/api/v1/zombie-guard/preview", {"paths": ["services/export/pdf_v1/render.py"]}, cookie=PM)
expect(pv["flagged"] and "Zombie-Guard" in pv["comment_markdown"], "Zombie-Guard preview flags a PR touching Legacy PDF")
s, d, _ = call("POST", f"/api/v1/sunset-plans/{next(p['id'] for p in mine if p['feature_key'] == 'zapier-v1-webhooks')}/advance",
               {}, cookie=ADM)
expect(s == 200 and d["cleanup_pr"]["provider"] == "sandbox", "demo admin opens the Zapier cleanup PR (sandbox GitHub only)")
s, d, _ = call("POST", f"/api/v1/sunset-plans/{d['id']}/cleanup-pr/simulate-merge", cookie=ADM)
expect(s == 200 and d.get("archived"), "merge -> archived")
s, dg, _ = call("POST", "/api/v1/reports/executive-digest", {}, cookie=PM)
z = dg["summary"]["zombie_tax"]["reclaimed_this_month"]
expect(s == 201 and dg["slack_status"] == "dry_run" and z["sunsets"] == 1 and z["eng_hours_month"] > 0,
       f"digest: {z['sunsets']} sunset, {z['eng_hours_month']} h/mo, {z['fte']} FTE, ${z['cost_month']:,.0f}/mo reclaimed (Slack dry run)")
s, html, _ = call("GET", "/api/v1/reports/executive-brief.html", cookie=PM)
expect(s == 200 and b"Executive Brief" in html, "print-ready Executive Brief (HTML)")
s, d, _ = call("POST", "/api/v1/demo/reset", cookie=PM)
expect(s == 200, "reset the demo for the next visitor")
print("\nPhase 5 walkthrough passed.")
