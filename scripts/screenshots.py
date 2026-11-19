"""Capture README screenshots of the live demo (CI: after the walkthrough, which leaves the demo freshly reset).

    pip install playwright && python -m playwright install --with-deps chromium
    python scripts/screenshots.py http://localhost:3000 docs/img
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000").rstrip("/")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/img")
OUT.mkdir(parents=True, exist_ok=True)


def token(role: str) -> str:
    req = urllib.request.Request(f"{BASE}/api/v1/demo/launch", method="POST", data=json.dumps({"role": role}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["token"]


SHOTS = [  # (file, path, role, full_page, action)
    ("demo-landing.png", "/login", None, False, None),
    ("demo-ledger.png", "/", "pm_tech_lead", True, None),
    ("demo-tearsheet-ai.png", "/features/ai-summaries", "pm_tech_lead", True, None),
    ("demo-zombie-guard.png", "/zombie-guard", "pm_tech_lead", False, "preview"),
    ("demo-sunset.png", "/sunset", "admin", False, None),
    ("demo-brief.png", "/reports", "admin", True, None),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, path, role, full, action in SHOTS:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        if role:
            ctx.add_cookies([{"name": "fl_session", "value": token(role), "url": BASE}])
        page = ctx.new_page()
        page.goto(BASE + path, wait_until="networkidle")
        if action == "preview":
            page.get_by_role("button", name="Preview the bot comment").click()
            page.wait_for_selector("[data-testid=guard-comment]")
        if name == "demo-landing.png":
            page.wait_for_selector("[data-testid=launch-demo]:not([disabled])")
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / name), full_page=full)
        print("saved", OUT / name)
        ctx.close()
    browser.close()
