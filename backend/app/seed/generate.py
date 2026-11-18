"""Deterministic seed data: "Northwind Cloud", a B2B document-workflow SaaS.

45 engineers in 7 teams, ~520 accounts (~$15M ARR), 22 features (4 shared-overhead), 6 months of
PRs, usage, tickets and cloud/LLM cost. The data is shaped so each classification shows up for a
reason you can read in the numbers:
  * legacy-pdf-export, zapier-v1-webhooks, bulk-csv-import-v1  -> Zombie (tiny, shrinking usage; bug tax)
  * saml-attribute-mapping                                     -> Niche Enterprise Anchor (Globex, $180k ARR)
  * gdpr-data-export                                           -> Mandated (regulatory)
  * ai-summaries                                               -> Growth Bet (launched 2026-04, LLM-heavy)
  * document-editor, realtime-collab, ...                      -> Cash Cow

PR file paths are generated under each feature's manifest globs and allocated through the REAL
manifest code path, so the seed exercises the same logic as live GitHub ingestion.

Usage:  python -m app.seed.generate --manifest ../features.yaml --out seed.sql
"""
from __future__ import annotations

import argparse
import json
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..core.effort import raw_effort_hours
from ..core.manifest import Manifest, parse_manifest
from ..core.types import (Account, CostLine, Dataset, Feature, OrgSettings, Team, Ticket, Usage, WorkItem,
                          WorkKind)

NS = uuid.UUID("6f1c2b1e-6a43-4b8e-9d0e-0f2d7f5b9a11")
ORG_SLUG = "northwind"
MONTHS = [date(2026, m, 1) for m in range(5, 11)]  # May..Oct 2026
AS_OF = date(2026, 10, 1)
AUTO_CONFIRM = 0.80  # org_settings.mapping_auto_confirm default: below this a human triages

TEAMS = [("editor", "Editor", 9), ("collaboration", "Collaboration", 7), ("docs-platform", "Docs Platform", 6),
         ("integrations", "Integrations", 6), ("ai", "AI", 5), ("enterprise", "Enterprise", 5),
         ("platform", "Platform", 7)]


@dataclass(frozen=True)
class Profile:
    adoption: float            # probability an eligible paying account uses it in a month
    days: float                # mean active days per month for an adopter
    pr_weight: float           # share of the owning team's PR throughput
    bug_share: float
    tickets: float             # tickets / month
    handle_min: float          # mean handle minutes per ticket
    infra: float               # tagged infra $/month
    llm: float = 0.0           # LLM $/month (grows with adoption for ai-summaries)
    trend: float = 0.0         # monthly multiplicative change in adoption (e.g. -0.12 = shrinking)


PROFILES: dict[str, Profile] = {
    "document-editor":        Profile(0.97, 18, 6.0, 0.25, 40, 35, 9_000),
    "realtime-collab":        Profile(0.85, 14, 4.0, 0.30, 25, 40, 7_000),
    "comments-mentions":      Profile(0.78, 10, 1.6, 0.20, 8, 25, 800),
    "templates-library":      Profile(0.62, 6, 1.4, 0.15, 5, 25, 300),
    "public-share-links":     Profile(0.60, 5, 0.9, 0.20, 6, 30, 400),
    "analytics-dashboard":    Profile(0.35, 3, 1.5, 0.20, 4, 30, 1_200),
    "e-signatures":           Profile(0.45, 5, 2.4, 0.20, 12, 40, 1_800),
    "pdf-export-v2":          Profile(0.55, 4, 1.8, 0.20, 9, 35, 2_200),
    "legacy-pdf-export":      Profile(0.045, 2, 1.8, 0.62, 38, 45, 410, trend=-0.12),
    "bulk-csv-import-v1":     Profile(0.03, 1, 1.0, 0.55, 15, 50, 200, trend=-0.10),
    "slack-integration":      Profile(0.45, 8, 1.2, 0.25, 6, 30, 250),
    "salesforce-sync":        Profile(0.50, 9, 2.2, 0.40, 14, 55, 900),
    "zapier-v1-webhooks":     Profile(0.03, 3, 1.4, 0.55, 12, 40, 650, trend=-0.15),
    "approval-workflows":     Profile(0.90, 8, 2.2, 0.20, 10, 40, 600),
    "saml-attribute-mapping": Profile(0.0, 2, 0.8, 0.50, 4, 60, 120),   # usage is hand-placed (Globex)
    "gdpr-data-export":       Profile(0.02, 1, 0.4, 0.30, 2, 60, 150),
    "ai-summaries":           Profile(0.05, 6, 5.0, 0.15, 10, 30, 4_000, llm=2_500, trend=0.45),
    "platform-auth-sso":      Profile(0.0, 0, 2.5, 0.30, 0, 0, 3_500),
    "design-system":          Profile(0.0, 0, 1.6, 0.10, 0, 0, 0),
    "billing-core":           Profile(0.0, 0, 1.2, 0.25, 0, 0, 900),
    "data-platform":          Profile(0.0, 0, 2.2, 0.20, 0, 0, 11_000),
}

ADJ = ["Blue", "North", "Bright", "Iron", "Silver", "Granite", "Summit", "Harbor", "Cedar", "Maple", "Pioneer",
       "Atlas", "Crescent", "Evergreen", "Falcon", "Golden", "Horizon", "Keystone", "Liberty", "Meridian",
       "Nimbus", "Orchid", "Pinnacle", "Quantum", "Redwood", "Sterling", "Trident", "Vertex", "Willow", "Zenith"]
NOUN = ["Logistics", "Health", "Capital", "Labs", "Foods", "Energy", "Legal", "Media", "Robotics", "Retail",
        "Insurance", "Biotech", "Freight", "Partners", "Analytics", "Studios", "Systems", "Holdings"]


def uid(*parts: object) -> str:
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def _path_under(glob: str, rng: random.Random, n: int) -> str:
    base = glob.split("*")[0].rstrip("/")
    ext = ".tsx" if base.startswith("web/") else ".py"
    sub = rng.choice(["", "handlers/", "models/", "lib/", "tests/"])
    name = f"test_m{n % 7}{ext}" if sub == "tests/" else f"m{n % 11}{ext}"
    return f"{base}/{sub}{name}"


@dataclass
class Seed:
    org_id: str
    dataset: Dataset
    manifest: Manifest
    sql: str                      # full demo: every fact pre-loaded (Phase 1 behaviour)
    sql_core: str = ""            # sandbox mode: org, teams, features, PRs; connectors bring the rest
    extras: dict = field(default_factory=dict)


def stripe_ref(account_id: str) -> str:
    return "cus_" + account_id.replace("-", "")[:14]


def account_domain(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum()) + ".com"


def pr_issue_key(external_id: str) -> str:
    return "NW-" + external_id.rsplit("#", 1)[1]


def build(manifest_text: str, rng_seed: int = 42) -> Seed:
    rng = random.Random(rng_seed)
    manifest = parse_manifest(manifest_text)
    specs = manifest.by_key()
    missing = set(specs) ^ set(PROFILES)
    if missing:
        raise ValueError(f"manifest and seed profiles disagree on features: {sorted(missing)}")
    org_id = uid("org", ORG_SLUG)
    settings = OrgSettings()

    team_rows = [(t.key, t.name, t.headcount) for t in manifest.teams] or TEAMS
    teams = [Team(uid("team", k), k, n, h) for k, n, h in team_rows]
    team_by_key = {t.key: t for t in teams}
    features = [Feature(
        id=uid("feature", s.key), key=s.key, name=s.name,
        owner_team_id=team_by_key[s.owner_team].id if s.owner_team else None,
        tier_gate=s.tier_gate, is_shared_overhead=s.shared_overhead, allocation_rule=s.allocation_rule,
        allocation_weights=s.allocation_weights, is_regulatory=s.regulatory,
        launched_at=date.fromisoformat(s.launched_at) if s.launched_at else None, flag_key=s.flag_key,
    ) for s in manifest.features]
    fid = {f.key: f.id for f in features}

    # ------------------------------------------------------------------ accounts
    accounts: list[Account] = []
    csm: dict[str, str] = {}
    names: set[str] = set()

    def new_name() -> str:
        while True:
            n = f"{rng.choice(ADJ)} {rng.choice(NOUN)}"
            if n not in names:
                names.add(n)
                return n

    def renewal() -> date:
        return AS_OF + timedelta(days=rng.randint(20, 360))

    def health() -> str:
        return rng.choices(["green", "yellow", "red"], [0.72, 0.2, 0.08])[0]

    fixed = [("Globex", "enterprise", 180_000, date(2026, 12, 15), "green"),
             ("Initech", "enterprise", 150_000, date(2027, 3, 1), "yellow"),
             ("Hooli", "enterprise", 210_000, date(2027, 6, 30), "green"),
             ("Umbrella Health", "enterprise", 165_000, date(2027, 1, 20), "green")]
    for name, tier, arr, ren, h in fixed:
        names.add(name)
        accounts.append(Account(uid("account", name), name, tier, arr, ren, h))
    tiers = ([("enterprise", 60_000, 240_000)] * 41 + [("pro", 18_000, 54_000)] * 160 +
             [("starter", 3_000, 9_600)] * 200 + [("free", 0, 0)] * 115)
    for tier, lo, hi in tiers:
        name = new_name()
        arr = round(rng.uniform(lo, hi) / 600) * 600.0 if hi else 0.0
        accounts.append(Account(uid("account", name), name, tier, arr, renewal() if arr else None, health()))
    csms = ["dana", "omer", "lior", "maya", "noa"]
    for a in accounts:
        if a.arr_usd:
            csm[a.id] = f"{csms[uuid.UUID(a.id).int % len(csms)]}@northwind.io"

    # ------------------------------------------------------------------ usage
    tier_rank = {"free": 0, "starter": 1, "pro": 2, "enterprise": 3}
    usage: list[Usage] = []
    act: dict[tuple[date, str], int] = {}
    adopter_seed = {(a.id, f.key): rng.random() for a in accounts for f in features}
    seats = {a.id: {"enterprise": 120, "pro": 25, "starter": 6, "free": 2}[a.plan_tier] for a in accounts}
    for mi, m in enumerate(MONTHS):
        for a in accounts:
            days_by_feature: dict[str, int] = {}
            for f in features:
                p = PROFILES[f.key]
                if f.is_shared_overhead or p.adoption <= 0:
                    continue
                if f.tier_gate and tier_rank[a.plan_tier] < tier_rank[f.tier_gate]:
                    continue
                if f.launched_at and f.launched_at > m:
                    continue
                adoption = min(p.adoption * (1 + p.trend) ** mi, 0.95)
                if adopter_seed[(a.id, f.key)] > adoption:
                    continue
                d = max(1, min(22, int(round(rng.gauss(p.days, max(p.days * 0.3, 0.6))))))
                users = max(1, int(seats[a.id] * min(1.0, adoption + 0.1) * rng.uniform(0.2, 0.6)))
                days_by_feature[f.key] = d
                usage.append(Usage(a.id, f.id, m, d * rng.randint(3, 40) * users, d, users))
            if a.name == "Globex":  # the niche anchor: SAML mapping on 14 of 22 active days
                days_by_feature["saml-attribute-mapping"] = 14
                usage.append(Usage(a.id, fid["saml-attribute-mapping"], m, 14 * 60, 14, 9))
            if a.name in ("Initech", "Hooli"):
                days_by_feature["saml-attribute-mapping"] = 2
                usage.append(Usage(a.id, fid["saml-attribute-mapping"], m, 30, 2, 1))
            if days_by_feature:
                act[(m, a.id)] = min(22, max(days_by_feature.values()) + rng.randint(0, 3))
                if a.name == "Globex":
                    act[(m, a.id)] = 22

    # ------------------------------------------------------------------ PRs
    work_items: list[WorkItem] = []
    wi_rows: list[dict] = []
    team_features: dict[str, list[str]] = {}
    for s in manifest.features:
        team_features.setdefault(s.owner_team, []).append(s.key)
    n = 0
    for m in MONTHS:
        for t in teams:
            keys = team_features[t.key]
            weights = [PROFILES[k].pr_weight for k in keys]
            for _ in range(_poisson(rng, t.headcount * 10.5)):
                n += 1
                key = rng.choices(keys, weights)[0]
                p = PROFILES[key]
                spec = specs[key]
                kind = WorkKind.BUG if rng.random() < p.bug_share else rng.choices(
                    [WorkKind.FEATURE, WorkKind.CHORE], [0.7, 0.3])[0]
                prio = None
                if kind is WorkKind.BUG and rng.random() < (0.12 if p.bug_share > 0.5 else 0.03):
                    kind, prio = WorkKind.HOTFIX, rng.choice(["P0", "P1"])
                size = int(rng.lognormvariate(4.2, 1.0))
                files = []
                for j in range(max(1, min(12, int(rng.lognormvariate(1.0, 0.7))))):
                    adds = max(1, int(size * rng.uniform(0.2, 1.0)))
                    files.append({"path": _path_under(rng.choice(spec.paths), rng, n + j), "additions": adds,
                                  "deletions": int(adds * rng.uniform(0, 0.6))})
                if rng.random() < 0.15:  # touches a second feature
                    other = specs[rng.choice(list(specs))]
                    if other.paths:
                        files.append({"path": _path_under(other.paths[0], rng, n), "additions": rng.randint(2, 40),
                                      "deletions": rng.randint(0, 10)})
                if rng.random() < 0.10:  # unmapped churn
                    files.append({"path": f"scripts/maintenance/job_{n % 9}.py", "additions": rng.randint(5, 60),
                                  "deletions": 0})
                if rng.random() < 0.05:
                    files.append({"path": "web/package-lock.json", "additions": 800, "deletions": 700})
                shares, _ = manifest.allocate_paths(files)
                counted = [f for f in files if not manifest.is_ignored(f["path"])]
                adds = sum(f["additions"] for f in counted)
                dels = sum(f["deletions"] for f in counted)
                review = min(6, _poisson(rng, 0.8 + (0.6 if kind is WorkKind.BUG else 0)))
                incident_h = round(rng.uniform(4, 16), 1) if prio == "P0" else 0.0
                day = rng.randint(1, 27)
                merged = datetime(m.year, m.month, day, rng.randint(8, 19), tzinfo=timezone.utc)
                wid = uid("wi", n)
                wi = WorkItem(wid, t.id, m, kind, prio, adds, dels, len(counted), review, incident_h,
                              is_sweeping=False, allocations={fid[k]: v for k, v in shares.items()})
                work_items.append(wi)
                wi_rows.append({"id": wid, "external_id": f"northwind/app#{10000 + n}", "team_id": t.id,
                                "kind": kind.value, "priority": prio,
                                "opened": merged - timedelta(hours=rng.randint(4, 120)), "merged": merged,
                                "additions": adds, "deletions": dels, "files": len(counted), "review": review,
                                "incident_h": incident_h, "paths": files, "shares": shares,
                                "raw": raw_effort_hours(adds, dels, len(counted), review, kind, prio, incident_h)})
        # one sweeping framework upgrade per month on the platform team: booked to overhead
        n += 1
        wid = uid("wi", n)
        merged = datetime(m.year, m.month, 15, 12, tzinfo=timezone.utc)
        work_items.append(WorkItem(wid, team_by_key["platform"].id, m, WorkKind.CHORE, None, 4200, 3900, 640, 2,
                                   is_sweeping=True))
        wi_rows.append({"id": wid, "external_id": f"northwind/app#{10000 + n}", "team_id": team_by_key["platform"].id,
                        "kind": "chore", "priority": None, "opened": merged - timedelta(days=3), "merged": merged,
                        "additions": 4200, "deletions": 3900, "files": 640, "review": 2, "incident_h": 0.0,
                        "paths": [], "shares": {}, "sweeping": True,
                        "raw": raw_effort_hours(4200, 3900, 640, 2, WorkKind.CHORE)})

    # ------------------------------------------------------------------ tickets
    tickets: list[Ticket] = []
    ticket_rows: list[dict] = []
    suggestions: list[dict] = []
    adopters: dict[tuple[date, str], list[str]] = {}
    for u in usage:
        adopters.setdefault((u.period_month, u.feature_id), []).append(u.account_id)
    tn = 0
    for m in MONTHS:
        for f in features:
            p = PROFILES[f.key]
            for _ in range(_poisson(rng, p.tickets)):
                tn += 1
                pool = adopters.get((m, f.id)) or [a.id for a in accounts if a.arr_usd]
                acct = rng.choice(pool)
                minutes = max(5, int(rng.lognormvariate(math.log(p.handle_min), 0.45)))
                created = datetime(m.year, m.month, rng.randint(1, 28), rng.randint(6, 20), tzinfo=timezone.utc)
                conf = 1.0
                src = "manifest"  # support tag matched
                mapped: str | None = f.id
                if rng.random() < 0.18:  # untagged: Tier 3 classifier
                    conf = round(rng.uniform(0.55, 0.97), 2)
                    src = "ai"
                    if conf < AUTO_CONFIRM:
                        mapped = None
                        suggestions.append({"subject": uid("ticket", tn), "feature": f.id, "conf": conf,
                                            "label": f"ZD-{70000 + tn} · {f.name.lower()} question"})
                tid = uid("ticket", tn)
                if mapped:
                    tickets.append(Ticket(tid, mapped, acct, m, minutes))
                ticket_rows.append({"id": tid, "external_id": f"ZD-{70000 + tn}", "account": acct, "feature": mapped,
                                    "src": src, "conf": conf, "created": created, "minutes": minutes,
                                    "solved": created + timedelta(hours=rng.randint(1, 72))})

    # ------------------------------------------------------------------ cost lines
    cost_lines: list[CostLine] = []
    for mi, m in enumerate(MONTHS):
        for f in features:
            p = PROFILES[f.key]
            if p.infra:
                cost_lines.append(CostLine(f.id, m, "infra", round(p.infra * rng.uniform(0.92, 1.08), 2)))
            if p.llm:
                cost_lines.append(CostLine(f.id, m, "llm", round(p.llm * (1 + p.trend) ** mi * rng.uniform(0.95, 1.05), 2)))
        cost_lines.append(CostLine(None, m, "infra", round(3_000 * rng.uniform(0.9, 1.1), 2)))

    snapshots = {m: accounts for m in MONTHS}
    ds = Dataset(settings, teams, features, work_items, snapshots, usage, act, tickets, cost_lines)
    args = (org_id, manifest, teams, features, accounts, csm, usage, act, wi_rows, ticket_rows, suggestions,
            cost_lines, fid)
    return Seed(org_id, ds, manifest, _to_sql(*args), _to_sql(*args, connector_data=False),
                {"accounts": accounts, "csm": csm, "ticket_rows": ticket_rows, "wi_rows": wi_rows})


# ---------------------------------------------------------------------- SQL rendering
def _q(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (date, datetime)):
        return f"'{v.isoformat()}'"
    if isinstance(v, (list, tuple)):
        return "ARRAY[" + ",".join(_q(x) for x in v) + "]::text[]" if v else "'{}'::text[]"
    return "'" + str(v).replace("'", "''") + "'"


def _insert(table: str, cols: list[str], rows: list[list[object]], chunk: int = 1000) -> list[str]:
    out = []
    for i in range(0, len(rows), chunk):
        vals = ",\n".join("(" + ",".join(_q(v) for v in r) + ")" for r in rows[i:i + chunk])
        out.append(f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n{vals};")
    return out


def _to_sql(org_id, manifest, teams, features, accounts, csm, usage, act, wi_rows, ticket_rows, suggestions,
            cost_lines, fid, connector_data: bool = True) -> str:
    """connector_data=False (SANDBOX_MODE): customers, usage, tickets and costs are NOT inserted here;
    the Phase 2 connectors fetch them from the sandbox APIs, exactly as they would from live ones."""
    s: list[str] = ["-- generated by app.seed.generate; deterministic (seed=42)", "BEGIN;",
                    f"SELECT set_config('app.org_id', '{org_id}', true);"]
    s += _insert("organizations", ["id", "name", "slug"], [[org_id, "Northwind Cloud", ORG_SLUG]])
    s += _insert("org_settings", ["org_id"], [[org_id]])
    s += _insert("users", ["id", "org_id", "email", "role"], [
        [uid("user", e), org_id, e, r] for e, r in [("admin@northwind.io", "admin"), ("cfo@northwind.io", "finance"),
                                                    ("pm.lead@northwind.io", "pm"), ("eng.lead@northwind.io", "eng_lead"),
                                                    ("dana@northwind.io", "cs")]])
    s += _insert("teams", ["id", "org_id", "key", "name", "headcount"],
                 [[t.id, org_id, t.key, t.name, t.headcount] for t in teams])
    gh = uid("integration", "github")
    sandbox = not connector_data
    status = "pending" if sandbox else "active"
    s += _insert("integrations", ["id", "org_id", "kind", "status", "display_name", "config", "sandbox"], [
        [gh, org_id, "github", "active", "GitHub · northwind", json.dumps({"repos": ["northwind/app"]}), False],
        [uid("integration", "jira"), org_id, "jira", status, "Jira", json.dumps({"projects": ["NW"]}), sandbox],
        [uid("integration", "posthog"), org_id, "posthog", status, "PostHog",
         json.dumps({"project_id": "1", "group_type_index": 0}), sandbox],
        [uid("integration", "stripe"), org_id, "stripe", status, "Stripe", "{}", sandbox],
        [uid("integration", "zendesk"), org_id, "zendesk", status, "Zendesk", "{}", sandbox],
        [uid("integration", "aws"), org_id, "aws_cost_explorer", status, "AWS Cost Explorer",
         json.dumps({"tag_key": "feature"}), sandbox]])
    repo = uid("repo", "northwind/app")
    s += _insert("repositories", ["id", "org_id", "integration_id", "full_name"], [[repo, org_id, gh, "northwind/app"]])
    by_key = manifest.by_key()
    s += _insert("features", ["id", "org_id", "key", "name", "description", "owner_team_id", "flag_key", "tier_gate",
                              "is_shared_overhead", "allocation_rule", "is_regulatory", "launched_at", "metadata"],
                 [[f.id, org_id, f.key, f.name, by_key[f.key].description, f.owner_team_id, f.flag_key, f.tier_gate,
                   f.is_shared_overhead, f.allocation_rule, f.is_regulatory, f.launched_at,
                   json.dumps({"replaced_by": by_key[f.key].replaced_by} if by_key[f.key].replaced_by else {})]
                  for f in features])
    assets, maps = [], []
    for spec in manifest.features:
        for t, vals in (("path_glob", spec.paths), ("api_route", spec.api_routes), ("analytics_event", spec.events),
                        ("feature_flag", (spec.flag_key,) if spec.flag_key else ())):
            for v in vals:
                aid = uid("asset", t, v)
                assets.append([aid, org_id, repo if t == "path_glob" else None, t, v])
                maps.append([aid, fid[spec.key], org_id, "manifest", 1.0, "confirmed"])
    s += _insert("code_assets", ["id", "org_id", "repo_id", "asset_type", "identifier"], assets)
    s += _insert("asset_feature_mappings", ["asset_id", "feature_id", "org_id", "source", "confidence", "status"], maps)
    # a few heuristic (Tier 2) mappings awaiting confirmation
    for path, key, conf in [("web/src/features/export-pdf-legacy-print/**", "legacy-pdf-export", 0.72),
                            ("services/notifications/digest/**", "comments-mentions", 0.64)]:
        aid = uid("asset", "path_glob", path)
        s += _insert("code_assets", ["id", "org_id", "repo_id", "asset_type", "identifier"],
                     [[aid, org_id, repo, "path_glob", path]])
        s += _insert("mapping_suggestions", ["org_id", "subject_type", "subject_id", "subject_label",
                                             "suggested_feature_id", "confidence", "source", "rationale"],
                     [[org_id, "code_asset", aid, path, fid[key], conf, "heuristic",
                       "Directory name and import graph cluster with the suggested feature"]])
    s += _insert("import_edges", ["org_id", "repo_id", "from_path", "to_path"], [
        [org_id, repo, "web/src/features/editor/toolbar.tsx", "web/src/features/export-pdf-legacy/button.tsx"],
        [org_id, repo, "services/editor/handlers/export.py", "services/export/pdf_v1/api.py"],
        [org_id, repo, "scripts/maintenance/nightly_reports.py", "services/export/pdf_v1/render.py"],
        [org_id, repo, "services/export/pdf_v1/api.py", "services/export/pdf_v1/render.py"],
        [org_id, repo, "services/integrations/slack/handlers/notify.py", "services/integrations/zapier_v1/lib/payload.py"],
        [org_id, repo, "services/importers/csv_v1/handlers/upload.py", "services/db/lib/bulk.py"],
        [org_id, repo, "services/sso/attribute_mapping/lib/apply.py", "services/auth/lib/session.py"],
        [org_id, repo, "services/auth/handlers/login.py", "services/sso/attribute_mapping/lib/apply.py"]])
    if connector_data:
        s += _connector_facts(org_id, accounts, csm, usage, act, ticket_rows, suggestions, cost_lines)
    s += _insert("work_items", ["id", "org_id", "source", "external_id", "item_type", "kind", "priority", "team_id",
                                "repo_id", "opened_at", "merged_at", "additions", "deletions", "files_changed",
                                "review_rounds", "is_revert", "is_sweeping", "incident_response_hours",
                                "raw_effort_hours", "path_stats", "linked_keys"],
                 [[w["id"], org_id, "github", w["external_id"], "pr", w["kind"], w["priority"], w["team_id"], repo,
                   w["opened"], w["merged"], w["additions"], w["deletions"], w["files"], w["review"], False,
                   w.get("sweeping", False), w["incident_h"], round(w["raw"], 2), json.dumps(w["paths"]),
                   [pr_issue_key(w["external_id"])]]
                  for w in wi_rows])
    s += _insert("work_item_allocations", ["work_item_id", "feature_id", "org_id", "share"],
                 [[w["id"], fid[k], org_id, v] for w in wi_rows for k, v in w["shares"].items() if v > 0])
    s += _sunset_seed(org_id, fid)
    s.append("COMMIT;")
    return "\n".join(s) + "\n"


def _connector_facts(org_id, accounts, csm, usage, act, ticket_rows, suggestions, cost_lines) -> list[str]:
    s: list[str] = []
    s += _insert("accounts", ["id", "org_id", "external_ref", "name", "plan_tier", "arr_usd", "renewal_date", "health",
                              "csm_email", "email_domain"],
                 [[a.id, org_id, stripe_ref(a.id), a.name, a.plan_tier, a.arr_usd, a.renewal_date,
                   a.health, csm.get(a.id), account_domain(a.name)] for a in accounts])
    s += _insert("account_snapshots", ["org_id", "account_id", "period_month", "arr_usd", "plan_tier", "health"],
                 [[org_id, a.id, m, a.arr_usd, a.plan_tier, a.health] for m in MONTHS for a in accounts])
    s += _insert("usage_signals", ["org_id", "period_month", "account_id", "feature_id", "events", "active_days",
                                   "active_users"],
                 [[org_id, u.period_month, u.account_id, u.feature_id, u.events, u.active_days, u.active_users]
                  for u in usage])
    s += _insert("account_activity_monthly", ["org_id", "period_month", "account_id", "active_days"],
                 [[org_id, m, a, d] for (m, a), d in act.items()])
    s += _insert("support_tickets", ["id", "org_id", "external_id", "account_id", "feature_id", "mapping_source",
                                     "mapping_confidence", "priority", "created_at", "solved_at", "handle_minutes"],
                 [[t["id"], org_id, t["external_id"], t["account"], t["feature"], t["src"], t["conf"], "normal",
                   t["created"], t["solved"], t["minutes"]] for t in ticket_rows])
    s += _insert("mapping_suggestions", ["org_id", "subject_type", "subject_id", "subject_label",
                                         "suggested_feature_id", "confidence", "source", "rationale"],
                 [[org_id, "support_ticket", x["subject"], x["label"], x["feature"], x["conf"], "ai",
                   "Semantic similarity to the feature's tickets and docs (embedding cosine)"] for x in suggestions])
    s += _insert("cost_events", ["org_id", "period_month", "feature_id", "cost_type", "source", "amount_usd", "unit"],
                 [[org_id, c.period_month, c.feature_id, c.cost_type,
                   "openai_usage" if c.cost_type == "llm" else "aws_cost_explorer", c.amount_usd, "usd"]
                  for c in cost_lines])
    return s


def _sunset_seed(org_id: str, fid: dict[str, str]) -> list[str]:
    """Two plans already in flight so the pipeline board is not empty on first run."""
    zap = uid("plan", "zapier-v1-webhooks")
    csv = uid("plan", "bulk-csv-import-v1")
    admin = uid("user", "admin@northwind.io")
    now = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
    canary = {"phase": "fully_off", "started_at": (now - timedelta(days=19)).isoformat(),
              "fully_off_at": (now - timedelta(days=12)).isoformat(), "rollbacks": 0}
    out = _insert("sunset_plans", ["id", "org_id", "feature_id", "state", "canary_phase", "reason", "canary",
                                   "created_by", "state_entered_at", "created_at"], [
        [zap, org_id, fid["zapier-v1-webhooks"], "canary_dark_launch", "fully_off",
         "Superseded by Zapier v2 app; 55% bug share", json.dumps(canary), admin, now - timedelta(days=19),
         now - timedelta(days=40)],
        [csv, org_id, fid["bulk-csv-import-v1"], "blast_radius_checked", None,
         "Replaced by the v2 importer in 2024", None, admin, now - timedelta(days=3), now - timedelta(days=6)]])
    steps = [(zap, None, "candidate_for_sunset", 40), (zap, "candidate_for_sunset", "blast_radius_checked", 36),
             (zap, "blast_radius_checked", "customer_outreach", 34), (zap, "customer_outreach", "canary_dark_launch", 19),
             (csv, None, "candidate_for_sunset", 6), (csv, "candidate_for_sunset", "blast_radius_checked", 3)]
    for plan, frm, to, ago in steps:
        out += _insert("sunset_audit_logs", ["org_id", "plan_id", "from_state", "to_state", "action", "actor_id",
                                             "payload", "created_at"],
                       [[org_id, plan, frm, to, "transition", admin, "{}", now - timedelta(days=ago)]])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(Path(__file__).resolve().parents[3] / "features.yaml"))
    ap.add_argument("--out", default="-")
    a = ap.parse_args()
    seed = build(Path(a.manifest).read_text())
    if a.out == "-":
        print(seed.sql, end="")
    else:
        Path(a.out).write_text(seed.sql)


if __name__ == "__main__":
    main()
