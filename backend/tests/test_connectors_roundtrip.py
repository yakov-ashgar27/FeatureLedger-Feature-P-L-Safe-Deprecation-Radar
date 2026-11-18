"""Sandbox APIs -> real connectors -> scrubbers -> normalizers -> P&L engine == the Phase 1 seed ledger.

This proves pagination, cursors, scrubbing, identity stitching and normalization together, with no
database and no network.
"""
import asyncio
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from app.connectors import normalize as N
from app.connectors.ratelimit import MemoryBucketStore
from app.connectors.registry import build
from app.connectors.stitching import AccountIndex
from app.core.pnl import compute_pnl
from app.core.types import Account, CostLine, Dataset, Ticket, Usage
from app.seed.generate import MONTHS, build as build_seed

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = (ROOT / "features.yaml").read_text()
PII = ["Jane Doe", "jane.doe@", "dev@northwind.io", "Dev Person", "PM Person", "requester", "assignee",
       "collaborator", "1 Main St", "+1 555", "my export is broken", "owner_email", "ceo@", "VIP contact",
       "Customer Jane"]


async def _sync(kind: str, cursors: dict[str, str] | None = None):
    b = build(kind, "org", f"int-{kind}", {"projects": ["NW"], "project_id": "1"}, True, MemoryBucketStore(), MANIFEST, env={})
    envs, cur = [], dict(cursors or {})
    async for batch in b.connector.sync(b.ctx, cur):
        envs.extend(batch.envelopes)
        if batch.cursor:
            cur[batch.stream] = batch.cursor
    return envs, cur


@pytest.fixture(scope="module")
def synced():
    out = {}
    for k in ("stripe", "zendesk", "posthog", "jira", "aws_cost_explorer"):
        out[k] = asyncio.run(_sync(k))
    return out


def latest(envs, entity):
    d = {}
    for e in envs:
        if e.entity_type == entity:
            d[e.external_id] = e.payload
    return list(d.values())


def test_no_pii_leaves_any_connector(synced):
    for kind, (envs, _) in synced.items():
        blob = json.dumps([e.payload for e in envs])
        for needle in PII:
            assert needle not in blob, f"{kind} leaked {needle!r}"


def test_volumes_and_cursors(synced):
    count = lambda k, t: sum(1 for e in synced[k][0] if e.entity_type == t)  # noqa: E731
    assert count("stripe", "stripe_customer") == 520
    assert count("stripe", "stripe_invoice") == 405 * 6
    assert count("jira", "jira_issue") == 2880 + 30   # + Phase 3 PRs in directories no manifest owns
    assert count("posthog", "posthog_group") == 523
    assert count("zendesk", "zendesk_ticket") > 1000
    assert set(synced["stripe"][1]) == {"customers", "subscriptions", "invoices", "events"}
    assert synced["posthog"][1]["usage"] == "2026-11-01"
    assert synced["aws_cost_explorer"][1]["costs"] == "2026-11-01"


def test_incremental_is_idempotent(synced):
    for kind in ("stripe", "jira", "zendesk", "aws_cost_explorer", "posthog"):
        envs, cur = synced[kind]
        keys = {e.idempotency_key for e in envs}
        again, _ = asyncio.run(_sync(kind, cur))
        assert {e.idempotency_key for e in again} <= keys, f"{kind} produced changed envelopes on re-read"


def test_round_trip_reproduces_seed_ledger(synced):
    from app.core.manifest import parse_manifest
    manifest = parse_manifest(MANIFEST)
    fx = {"usd": 1.0}
    st = synced["stripe"][0]
    accounts = N.stripe_accounts(latest(st, "stripe_customer"), latest(st, "stripe_subscription"),
                                 latest(st, "stripe_invoice"), fx)
    idx = AccountIndex()
    for a in accounts:
        idx.add_account(a.external_ref, a.external_ref, a.email_domain)
    gm = N.group_matches(latest(synced["posthog"][0], "posthog_group"), idx)
    assert sum(1 for x in gm.values() if x.account_id) == 520
    assert {k for k, x in gm.items() if not x.account_id} == {"grp_trial_bluefin", "grp_trial_kestrel", "grp_personal_gmail"}
    om = N.org_matches(latest(synced["zendesk"][0], "zendesk_org"), idx)
    assert all(x.account_id for x in om.values())

    by_month = defaultdict(list)
    for a in accounts:
        for mo, arr in a.snapshots.items():
            by_month[mo].append(Account(a.external_ref, a.name, a.plan_tier, arr, a.renewal_date, a.health))
    ph = synced["posthog"][0]
    usage, act = [], {}
    for mo in MONTHS:
        um = N.usage_month(mo, [p for p in latest(ph, "posthog_usage") if p["month"] == mo.isoformat()],
                           [p for p in latest(ph, "posthog_activity") if p["month"] == mo.isoformat()], manifest,
                           {k: v.account_id for k, v in gm.items()}, "__unresolved__")
        fid = {f.key: f.id for f in build_seed(MANIFEST).dataset.features}
        for (acct, fk), v in um.usage.items():
            usage.append(Usage(acct, fid[fk], mo, v["events"], v["active_days"], v["active_users"]))
        for acct, d in um.activity.items():
            act[(mo, acct)] = d
    seed = build_seed(MANIFEST)
    fid = {f.key: f.id for f in seed.dataset.features}
    tickets = []
    for t in latest(synced["zendesk"][0], "zendesk_ticket"):
        r = N.ticket_row(t, manifest, {k: v.account_id for k, v in om.items()})
        if r.feature_key:
            tickets.append(Ticket(r.external_id, fid[r.feature_key], r.account_id, date(r.created_at.year, r.created_at.month, 1),
                                  r.handle_minutes))
    costs = []
    aws = synced["aws_cost_explorer"][0]
    for mo in MONTHS:
        for (fk, ct), amt in N.cost_month([p for p in latest(aws, "aws_cost") if p["month"] == mo.isoformat()],
                                          manifest).items():
            costs.append(CostLine(fid[fk] if fk else None, mo, ct, amt))

    ds = seed.dataset
    rebuilt = Dataset(ds.settings, ds.teams, ds.features, ds.work_items, dict(by_month), usage, act, tickets, costs)
    want = compute_pnl(ds, MONTHS)
    got = compute_pnl(rebuilt, MONTHS)
    for (mo, f), w in want.rows.items():
        g = got.rows[(mo, f)]
        assert g.classification == w.classification, (w.feature_key, mo)
        assert g.c_total == pytest.approx(w.c_total, abs=0.05), (w.feature_key, mo)
        assert g.v_usage == pytest.approx(w.v_usage, abs=0.05), (w.feature_key, mo)
        assert g.revenue_at_risk_arr == pytest.approx(w.revenue_at_risk_arr, abs=0.5), (w.feature_key, mo)
