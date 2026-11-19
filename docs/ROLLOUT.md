# Rollout log: from sandbox to production

The path from the sandbox demo to a live deployment, in three stages, and what each one taught us.
Every finding that needed a code change links to it. Newest entries last.

| Stage | What | Status |
| --- | --- | --- |
| 1 | Real GitHub data (read-only token), then Zombie-Guard comments and a cleanup PR on a test repository | in progress |
| 2 | Stripe (test mode), PostHog (free tier), Slack webhook | not started |
| 3 | Production: domain + HTTPS, SSO (Google / Auth0), secrets manager, database backups, several API workers, alerts, LaunchDarkly / Statsig | not started |

## Before Stage 1: readiness work (2026-11-19)

Done before any real data was connected (details in [SPEC §13.6](SPEC.md)): tenant-isolation proof suite,
confidence and coverage per feature, security hardening (revocable sessions, rate limits, security headers,
LLM consent, dependency audit), sunset damage signals, glossary and a `features.yaml` draft generator.

## Stage 1: real GitHub data

**Setup (Windows 11, Docker Desktop, 2026-11-19).** Product repository: `yakov-ashgar27/wifi-doctor`
(FastAPI + React, one developer). FeatureLedger runs in real-data mode with dev login:

```
SEED_ON_START=false   DEFAULT_ORG_SLUG=wifi-doctor   ORG_NAME=WiFi-Doctor
SANDBOX_MODE=true     DEMO_MODE=false                BOOTSTRAP_ADMIN_EMAILS=admin@wifi-doctor.local
GITHUB_TOKEN=<fine-grained, this repository only: Contents read-only, Pull requests read-only>
```

`features.yaml` for wifi-doctor was written by hand from its code: 7 features (live telemetry, root cause
analysis, incident log, stress test, chaos simulator, topology map, Wi-Fi radio stats) plus shared platform code.
It is kept in the product repository, next to the code it describes.

Sync on Windows: `powershell -ExecutionPolicy Bypass -File .\scripts\sync-github.ps1 -Repo yakov-ashgar27/wifi-doctor -Team core`

**Findings**

1. *The draft generator did not recognise the layout.* wifi-doctor groups code by layer (`engine/`, `routers/`,
   `components/`), not by feature folder, so `draft_manifest` found no feature folders. The manifest was written
   by hand. Open: a file-level mode for layer-organised repositories.
2. *Ports 8000 and 3000 were already taken by other local apps.* The ports are now settings
   (`API_PORT`, `WEB_PORT` in `.env`; `docker-compose.yml`, `Makefile`).
3. *Nothing was counted: every commit was pushed straight to `main`, there were no pull requests.* FeatureLedger
   only counted merged PRs. Direct pushes to the default branch are now work items of type `commit`
   (migration `0011_direct_commits.sql`, `app/ingest/github.py`); merge commits and commits that belong to any PR
   are skipped so nothing is counted twice. Tested against real Postgres with a fake GitHub
   (`tests/test_github_commits_api.py`).
4. *Commits were stored but not counted: engineering cost $0 on every feature.* The ingest change in (3) was
   incomplete: the P&L loader, the manifest re-allocation, the Tier 2/3 classifiers and the Jira link checks still
   read `item_type = 'pr'` only. A diagnostic query (`diag.sql`: teams, work items with allocations, capacity) showed
   10 commits correctly mapped and `raw_hours = 0` in the capacity row. All readers now take `('pr', 'commit')`, and
   the integration test now checks that the P&L loader sees the commit, not just that it was stored.
5. *After the fix in (4) the API would not start:* `allocations sum to 1.0000 > 1`. Shares are stored as
   `numeric(6,5)` and were rounded one by one, so six shares of one commit summed to 1.00002 and the effort model's
   guard stopped the whole recompute (and with it the API's startup). Synthetic data never hit this. Shares are now
   rounded so their sum never exceeds the exact total (`round_shares`), and stored shares that are over by rounding
   only (≤ 0.001) are rescaled instead of refused. Lesson: one bad row must not take the service down; tracked as
   a follow-up (recompute failures should be logged and reported, not fatal at startup).
6. *No `make` on Windows.* `scripts/sync-github.ps1` does the sync and prints the ledger with PowerShell only.

**Still to do in Stage 1:** check the ledger and tear-sheets on the real data; Zombie-Guard comments and a
cleanup PR on a test branch of the product repository.
