# FeatureLedger: Feature P&L & Safe Deprecation Radar

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/yakov-ashgar27/FeatureLedger-Feature-P-L-Safe-Deprecation-Radar?quickstart=1)
[![ci](https://github.com/yakov-ashgar27/FeatureLedger-Feature-P-L-Safe-Deprecation-Radar/actions/workflows/ci.yml/badge.svg)](https://github.com/yakov-ashgar27/FeatureLedger-Feature-P-L-Safe-Deprecation-Radar/actions/workflows/ci.yml)

**Run it with one click:** press *Open in GitHub Codespaces*. The whole stack builds and starts in the cloud, and the dashboard opens by itself in 3–6 minutes. Nothing to install.

FeatureLedger gives every in-house product feature a monthly **profit & loss** statement. It joins
engineering effort (Git), support load, cloud and LLM spend, and tenant-level usage and ARR.
It then turns a "Zombie" verdict into a **guarded, reversible deletion workflow**.

**מדריך הפעלה מלא בעברית, צעד אחר צעד:** [`docs/GETTING_STARTED.he.md`](docs/GETTING_STARTED.he.md)

This is **Phase 1 (MVP v0.1)**. It runs locally out of the box with a realistic seed company:
*Northwind Cloud*, 45 engineers in 7 teams, ~520 accounts, ~$14M ARR, 22 features and 6 months of history.

```bash
cp .env.example .env
docker compose up --build        # first start: migrate → seed → compute P&L (≈30 s)
open http://localhost:3000       # dashboard
open http://localhost:8000/docs  # API (OpenAPI)
```

## What you'll see on the seed data

| Feature | Class | Why |
| --- | --- | --- |
| Legacy PDF Exporter | **Zombie** | 2.5% adoption and shrinking, ~75% bug tax, ≈ $24k/mo net loss |
| Zapier v1 Webhooks, Bulk CSV Import v1 | **Zombie** | Tiny usage, high maintenance |
| Custom SAML Attribute Mapping | **Niche Anchor** | <7% adoption, but Globex ($180k ARR, renews in Dec) uses it on 14 of 22 days |
| GDPR Data Export | **Mandated** | Regulatory: never a sunset candidate |
| AI Summaries | **Growth Bet** | Launched 2026-04, adoption +48%/mo, LLM-heavy |
| Document Editor, Real-time Collab, … | **Cash Cow** | High value at a manageable cost |

The What-if Simulator pre-selects the three zombies. It shows roughly **4.6 FTE** (515 engineering hours a month)
and **~$630k/yr** of avoidable cost, against ~$115k of expected ARR loss across 31 accounts. Two sunset plans are already in
flight. Zapier v1 is in its scream test; try **Roll back**, or start a new plan from the
Legacy PDF tear-sheet.

## Repository layout

```
db/migrations/        PostgreSQL DDL: partitions, RLS, hash-chained audit log, optional pgvector
backend/app/core/     pure P&L engine: effort, attribution, pnl, manifest, sunset, simulate
backend/app/ingest/   GitHub ingester + allowlist scrubber (no author identity, no source code)
backend/app/jobs/     P&L job: DB → engine → ledger tables
backend/app/api/      FastAPI routes
backend/app/seed/     deterministic Northwind Cloud generator
backend/tests/        48 tests: formulas, guards, scrubber, seed end-to-end invariants
web/                  Next.js 15 dashboard: ledger + quadrant, tear-sheet, simulator, sunset board, triage
features.yaml         Tier 1 Feature Graph manifest (teams + features) for the seed company
templates/            CSV templates: accounts, usage, tickets, costs
docs/SPEC.md          formulas, state machine, API, design decisions
```

## Development

```bash
make test                         # backend tests, no database required
cd backend && python -m app.migrate   # migrate/seed against ADMIN_DATABASE_URL
cd web && npm install && npm run dev  # dashboard on :3000 (API_URL defaults to localhost:8000)
```

### Use your own data

1. Describe your teams and features in `features.yaml`.
2. In `.env`, set `SEED_ON_START=false`, `DEFAULT_ORG_SLUG` and `ORG_NAME`.
3. Run `docker compose down -v && docker compose up --build -d`.
4. Import CSVs from `templates/`: `make import KIND=accounts FILE=accounts.csv PERIOD=2026-10`. Do the same for `usage`, `tickets` and `costs`, then run `make pnl`.

Until accounts and usage are imported, every feature is classified **Watch**: cost alone never makes a Zombie.

### Ingest a real repository

Put your own `features.yaml` in the repo root, set `GITHUB_TOKEN` (a fine-grained, read-only token
with Pull requests: read), then run:

```bash
make sync-github REPO=your-org/your-repo
```

Merged PRs are scrubbed, allocated to features through the manifest, and calibrated against team headcount.
PRs that match no manifest path go to the triage queue.

## Principles

- **No surveillance.** No author, reviewer or assignee is stored anywhere. The schema has no column
  for them. Teams under 3 people are rolled up.
- **Conservation.** Feature + overhead + unmapped + unattributed hours equal real team capacity, every month.
- **Two lenses.** Additive value (sums to MRR) for the ledger, and counterfactual revenue-at-risk to guard deletions.
- **Reversible by default.** No step deletes code until a feature-flag scream test has been quiet for N days.

See [`docs/SPEC.md`](docs/SPEC.md) for every formula and threshold.
