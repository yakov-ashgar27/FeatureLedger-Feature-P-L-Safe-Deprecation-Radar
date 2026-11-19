"""Phase 4 demo walkthrough against a running stack (docker compose up), through the dashboard's own
origin so the Next.js proxy, cookies and CSRF header are exercised exactly as a browser uses them.

    python3 scripts/e2e_phase4.py [http://localhost:3000]

Standard library only. Exits non-zero on the first broken expectation.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000").rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def call(method: str, path: str, body=None, token: str | None = None, cookie: str | None = None,
         headers: dict | None = None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if cookie:
        h["Cookie"] = f"fl_session={cookie}"
        if method != "GET":
            h["X-FL-CSRF"] = "1"
    req = urllib.request.Request(BASE + path, method=method, headers=h,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with OPENER.open(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return e.code, data, e.headers


def expect(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def code(detail) -> str | None:
    d = detail.get("detail") if isinstance(detail, dict) else None
    return d.get("code") if isinstance(d, dict) else None


def main() -> None:
    # ---------------------------------------------------------------- sign in (dev login, SANDBOX_MODE)
    s, cfg, _ = call("GET", "/api/v1/auth/config")
    expect(s == 200 and cfg["dev_login"] is True, f"auth config: dev login on, oidc={cfg['oidc']}")
    s, _, hdr = call("GET", "/")
    expect(s in (302, 307) and "/login" in (hdr.get("Location") or ""), "anonymous dashboard request redirects to /login")
    s, page, _ = call("GET", "/login")
    expect(s == 200, "login page renders")

    tok = {}
    for role in ("admin", "pm_tech_lead", "cs_lead", "viewer"):
        s, d, hdr = call("POST", "/api/v1/auth/dev-login", {"role": role})
        expect(s == 200 and "fl_session=" in (hdr.get("Set-Cookie") or ""), f"dev login as {role} sets the session cookie via the proxy")
        tok[role] = d["token"]
    A, P, C, V = tok["admin"], tok["pm_tech_lead"], tok["cs_lead"], tok["viewer"]

    for path in ("/", "/features/legacy-pdf-export", "/simulator", "/sunset", "/mappings", "/integrations", "/settings"):
        s, html, _ = call("GET", path, cookie=V)
        expect(s == 200 and b"FeatureLedger" in html, f"page {path} renders for a signed-in viewer")
    s, html, _ = call("GET", "/", cookie=A)
    expect(b"Admin" in html, "the shell shows the signed-in role")

    s, me, _ = call("GET", "/api/v1/auth/me", cookie=V)
    expect(s == 200 and me["permissions"] == ["read"], "viewer session: read-only permissions")
    s, d, _ = call("PUT", "/api/v1/settings", {"eng_hourly_rate_usd": 120}, cookie=V)
    expect(s == 403 and code(d) == "forbidden", "viewer cannot change org settings (403)")
    s, d, _ = call("PUT", "/api/v1/settings", {"eng_hourly_rate_usd": 120}, token=None,
                   headers={"Cookie": f"fl_session={A}"})
    expect(s == 403 and code(d) == "csrf", "cookie write without X-FL-CSRF is refused")
    s, d, _ = call("GET", "/api/v1/pnl?window=3")
    expect(s == 401, "API without a session: 401")

    # ---------------------------------------------------------------- zapier: scream test -> PR -> merge
    s, plans, _ = call("GET", "/api/v1/sunset-plans", cookie=V)
    zap = next(p for p in plans if p["feature_key"] == "zapier-v1-webhooks")
    expect(zap["state"] == "canary_dark_launch" and zap["flag"]["state"] == "fully_off", "zapier plan is in a fully-off canary")
    s, st, _ = call("GET", f"/api/v1/sunset-plans/{zap['id']}/scream-test", cookie=V)
    expect(s == 200 and st["passes"], f"scream test quiet: {st['quiet_days']}/{st['required_days']} days, 0 'on', 0 tickets")

    s, d, _ = call("POST", f"/api/v1/sunset-plans/{zap['id']}/advance", {}, cookie=P)
    expect(s == 403, "a PM / tech lead cannot approve the cleanup PR")
    call("POST", "/api/v1/flags/zapier-v1/evaluations", {"on_count": 1}, cookie=A)
    s, d, _ = call("POST", f"/api/v1/sunset-plans/{zap['id']}/advance", {}, cookie=A)
    expect(s == 409 and code(d) == "flag_still_served", "an 'on' evaluation during the scream test -> 409 flag_still_served")
    call("POST", "/api/v1/flags/zapier-v1/evaluations", {"reset": True}, cookie=A)
    s, d, _ = call("POST", f"/api/v1/sunset-plans/{zap['id']}/advance", {}, cookie=A)
    expect(s == 200 and d["state"] == "dead_code_pr_opened", "admin approves: cleanup PR opened")
    pr = d["cleanup_pr"]
    expect(pr["branch"] == "sunset/zapier-v1-webhooks" and len(pr["files_deleted"]) > 0,
           f"PR {pr['repo']}#{pr['number']} on {pr['branch']} deletes {len(pr['files_deleted'])} files")

    s, ints, _ = call("GET", "/api/v1/integrations", cookie=A)
    gh = next(i for i in ints["integrations"] if i["kind"] == "github")
    payload = {"action": "closed", "number": pr["number"], "repository": {"full_name": pr["repo"]},
               "pull_request": {"number": pr["number"], "merged": True, "merge_commit_sha": "e2e" + "0" * 37,
                                "head": {"ref": pr["branch"]}}}
    s, d, _ = call("POST", f"/api/v1/webhooks/github/{gh['id']}", payload,
                   headers={"X-GitHub-Event": "pull_request", "X-FeatureLedger-Secret": "sandbox"})
    expect(s == 202 and d.get("archived") is True, "GitHub pull_request (merged) webhook archives the plan")
    s, z, _ = call("GET", f"/api/v1/sunset-plans/{zap['id']}", cookie=V)
    expect(z["state"] == "archived" and z["audit_log"][-1]["payload"]["actor"] == "github-webhook",
           "plan archived, audited as the webhook")

    # ---------------------------------------------------------------- legacy pdf: canary + rollback
    s, d, _ = call("POST", "/api/v1/sunset-plans", {"feature_key": "legacy-pdf-export",
                                                    "reason": "Superseded by PDF export v2", "scream_test_days": 7}, cookie=P)
    expect(s == 201, "PM starts a sunset for legacy-pdf-export")
    pid = d["id"]
    for step, who, body in [("evidence/blast_radius", P, None), ("acknowledge", P, {"kind": "inbound", "ids": ["*"]}),
                            ("advance", P, {}), ("evidence/impact_dossier", C, None),
                            ("acknowledge", C, {"kind": "account", "ids": ["*"]}), ("advance", C, {"cs_signoff": True})]:
        s, d, _ = call("POST", f"/api/v1/sunset-plans/{pid}/{step}", body, cookie=who)
        expect(s == 200, f"{step} ({'pm' if who == P else 'cs'}) -> {d.get('state', 'ok') if isinstance(d, dict) else s}")
    s, d, _ = call("POST", f"/api/v1/sunset-plans/{pid}/advance", {}, cookie=A)
    expect(s == 200 and d["canary_phase"] == "zero_usage_off", f"admin dark-launches: flag off for 0-usage accounts, kept on for {d['canary'].get('keep_on_for_count')}")
    s, d, _ = call("POST", f"/api/v1/sunset-plans/{pid}/advance", {}, cookie=A)
    expect(s == 200 and d["canary_phase"] == "fully_off", "admin promotes: flag fully off, scream test starts")
    s, d, _ = call("POST", f"/api/v1/sunset-plans/{pid}/advance", {}, cookie=A)
    expect(s == 409 and code(d) == "scream_test_running", "advancing during the scream test -> 409 scream_test_running")
    s, d, _ = call("POST", f"/api/v1/sunset-plans/{pid}/rollback", {"reason": "Enterprise escalation"}, cookie=C)
    expect(s == 200 and d["state"] == "customer_outreach" and d["rollback_count"] == 1, "CS lead rolls back: back to outreach, rollback_count 1")
    s, flags, _ = call("GET", "/api/v1/flags", cookie=V)
    f = next(x for x in flags["flags"] if x["flag_key"] == "legacy-pdf-export")
    expect(f["state"] == "fully_on", "flag legacy-pdf-export is fully on again")
    s, g, _ = call("GET", f"/api/v1/sunset-plans/{pid}", cookie=V)
    log = g["audit_log"]
    expect(all(b["prev_hash"] == a["hash"] for a, b in zip(log, log[1:])), f"audit log hash chain intact ({len(log)} entries)")
    print("\nPhase 4 walkthrough passed.")


if __name__ == "__main__":
    main()
