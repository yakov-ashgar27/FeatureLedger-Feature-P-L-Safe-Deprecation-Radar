"""Inbox processors: pending envelopes -> canonical tables, in bulk, with dead-letter isolation.

Run inside an org-scoped transaction. Order matters (accounts before anything that stitches to them):
  stripe_customer/subscription/invoice -> zendesk_org, posthog_group (stitching) -> jira_issue ->
  zendesk_ticket -> posthog_usage/activity (month rebuild) -> aws_cost (month rebuild)

Isolation: a payload that fails to normalize fails alone. A handler whose SQL fails is rolled back to its
savepoint and all of its envelopes are marked failed. Every failure increments `attempts`; at 5 the
envelope is dead-lettered (`dead_lettered_at`) and never blocks its stream again. The Sync Health view
lists dead letters and can re-queue them.

Rebuild-style sources (Stripe accounts, PostHog months, AWS months) recompute their output from the
LATEST version of every envelope of that entity, so replays and restatements converge.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..core.manifest import Manifest
from . import normalize as N
from .stitching import UNRESOLVED_REF, AccountIndex

log = logging.getLogger("featureledger.processors")
MAX_ATTEMPTS = 5
ORDER = ["stripe_customer", "stripe_subscription", "stripe_invoice", "zendesk_org", "posthog_group", "jira_issue",
         "zendesk_ticket", "posthog_usage", "posthog_activity", "aws_cost"]
TICKET_NS = uuid.UUID("0d1c9f3e-2b7a-4c55-9a51-6c1f0e2d8b44")

LATEST_SQL = """SELECT DISTINCT ON (external_id) external_id, payload FROM ingest_inbox
                 WHERE entity_type = $1 AND {cond} ORDER BY external_id, received_at DESC"""


@dataclass
class Stats:
    processed: int = 0
    failed: int = 0
    dead_lettered: int = 0
    accounts_upserted: int = 0
    months_rebuilt: list[str] = field(default_factory=list)
    unresolved_groups: int = 0
    unknown_events: int = 0
    unmatched_orgs: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


async def _latest(conn, entity: str, cond: str, *args) -> list[dict[str, Any]]:
    return [r["payload"] for r in await conn.fetch(LATEST_SQL.format(cond=cond), entity, *args)]


async def load_index(conn) -> AccountIndex:
    idx = AccountIndex()
    for r in await conn.fetch("SELECT id, external_ref, email_domain FROM accounts"):
        idx.add_account(str(r["id"]), r["external_ref"], r["email_domain"])
    for r in await conn.fetch("SELECT system::text AS system, external_id, account_id FROM account_identities"):
        idx.add_identity(r["system"], r["external_id"], str(r["account_id"]))
    return idx


async def unresolved_account(conn, org_id: str) -> str:
    return str(await conn.fetchval("""
        INSERT INTO accounts (org_id, external_ref, name, plan_tier, arr_usd, health)
        VALUES ($1, $2, 'Unresolved (unmatched usage)', 'free', 0, 'green')
        ON CONFLICT (org_id, external_ref) DO UPDATE SET name = EXCLUDED.name RETURNING id""", org_id, UNRESOLVED_REF))


# ------------------------------------------------------------------------------------------ Stripe
async def _stripe(conn, org_id: str, envs: list[dict], fx: dict[str, float], st: Stats,
                  failed: dict[str, str]) -> bool:
    customers: set[str] = set()
    owner: dict[str, list[str]] = defaultdict(list)
    for e in envs:
        p = e["payload"]
        c = p.get("id") if e["entity_type"] == "stripe_customer" else p.get("customer")
        if not c:
            failed[e["idempotency_key"]] = "payload has no customer id"
            continue
        customers.add(c)
        owner[c].append(e["idempotency_key"])
    if not customers:
        return False
    ids = sorted(customers)
    cust = await _latest(conn, "stripe_customer", "external_id = ANY($2)", ids)
    subs = await _latest(conn, "stripe_subscription", "payload->>'customer' = ANY($2)", ids)
    invs = await _latest(conn, "stripe_invoice", "payload->>'customer' = ANY($2)", ids)
    accounts: list[N.StripeAccount] = []
    for c in cust:
        try:
            accounts += N.stripe_accounts([c], [s for s in subs if s["customer"] == c["id"]],
                                          [i for i in invs if i["customer"] == c["id"]], fx)
        except Exception as ex:  # noqa: BLE001 - isolate one bad customer
            for k in owner.get(c.get("id", ""), []):
                failed[k] = f"stripe normalize: {ex}"[:500]
    before = await conn.fetchval("SELECT count(*) FROM accounts")
    await conn.executemany("""
        INSERT INTO accounts (org_id, external_ref, name, plan_tier, arr_usd, renewal_date, health, csm_email, email_domain)
        VALUES ($1, $2, $3, $4, $5, $6, $7::account_health, $8, $9)
        ON CONFLICT (org_id, external_ref) DO UPDATE SET name = EXCLUDED.name, plan_tier = EXCLUDED.plan_tier,
            arr_usd = EXCLUDED.arr_usd, renewal_date = EXCLUDED.renewal_date, health = EXCLUDED.health,
            csm_email = coalesce(EXCLUDED.csm_email, accounts.csm_email),
            email_domain = coalesce(EXCLUDED.email_domain, accounts.email_domain)""",
        [(org_id, a.external_ref, a.name, a.plan_tier, a.arr_usd, a.renewal_date, a.health, a.csm_email,
          a.email_domain) for a in accounts])
    st.accounts_upserted += len(accounts)
    ref_to_id = {r["external_ref"]: r["id"] for r in await conn.fetch(
        "SELECT id, external_ref FROM accounts WHERE external_ref = ANY($1)", [a.external_ref for a in accounts])}
    await conn.executemany("""INSERT INTO account_identities (account_id, org_id, system, external_id)
                              VALUES ($1, $2, 'stripe', $3) ON CONFLICT (org_id, system, external_id) DO NOTHING""",
                           [(ref_to_id[a.external_ref], org_id, a.external_ref) for a in accounts])
    await conn.executemany("""
        INSERT INTO account_snapshots (org_id, account_id, period_month, arr_usd, plan_tier, health)
        VALUES ($1, $2, $3, $4, $5, $6::account_health)
        ON CONFLICT (org_id, period_month, account_id) DO UPDATE SET arr_usd = EXCLUDED.arr_usd,
            plan_tier = EXCLUDED.plan_tier, health = EXCLUDED.health""",
        [(org_id, ref_to_id[a.external_ref], m, arr, a.plan_tier, a.health)
         for a in accounts for m, arr in a.snapshots.items()])
    after = await conn.fetchval("SELECT count(*) FROM accounts")
    return after != before


# ------------------------------------------------------------------------------------------ stitching
async def _stitch(conn, org_id: str, index: AccountIndex, kind: str, payloads: list[dict], st: Stats) -> None:
    if kind == "posthog":
        matches = N.group_matches(payloads, index)
    else:
        matches = N.org_matches(payloads, index)
    rows = [(m.account_id, org_id, kind, ext) for ext, m in matches.items() if m.account_id and m.method == "domain"]
    await conn.executemany("""INSERT INTO account_identities (account_id, org_id, system, external_id)
                              VALUES ($1, $2, $3::integration_kind, $4)
                              ON CONFLICT (org_id, system, external_id) DO NOTHING""", rows)
    for acct, _, _, ext in rows:
        index.add_identity(kind, ext, acct)
    unmatched = sum(1 for m in matches.values() if not m.account_id)
    if kind == "posthog":
        st.unresolved_groups += unmatched
    else:
        st.unmatched_orgs += unmatched


# ------------------------------------------------------------------------------------------ Jira
async def _jira(conn, org_id: str, envs: list[dict], manifest: Manifest, failed: dict[str, str]) -> None:
    rows, labelled = [], []
    for e in envs:
        try:
            r = N.jira_row(e["payload"], manifest)
            rows.append((org_id, r["key"], r["kind"], r["priority"], r["created"], r["resolved"], r["labels"]))
            if r["feature_key"]:
                labelled.append((r["key"], r["feature_key"]))
        except Exception as ex:  # noqa: BLE001
            failed[e["idempotency_key"]] = f"jira normalize: {ex}"[:500]
    await conn.executemany("""
        INSERT INTO work_items (org_id, source, external_id, item_type, kind, priority, opened_at, merged_at, labels)
        VALUES ($1, 'jira', $2, 'issue', $3::work_kind, $4, coalesce($5::timestamptz, now()), $6::timestamptz, $7)
        ON CONFLICT (org_id, source, external_id) DO UPDATE SET kind = EXCLUDED.kind, priority = EXCLUDED.priority,
            merged_at = EXCLUDED.merged_at, labels = EXCLUDED.labels""", rows)
    # a PR linked to a Bug/Incident is a bug fix even if its title did not say so
    await conn.execute("""
        UPDATE work_items pr SET kind = iss.kind, priority = coalesce(pr.priority, iss.priority)
          FROM work_items iss
         WHERE iss.source = 'jira' AND iss.item_type = 'issue' AND pr.item_type = 'pr'
           AND iss.external_id = ANY(pr.linked_keys) AND iss.kind IN ('bug', 'incident')
           AND pr.kind IN ('feature', 'chore')""")
    await conn.execute("""
        UPDATE work_items pr SET priority = iss.priority
          FROM work_items iss
         WHERE iss.source = 'jira' AND iss.item_type = 'issue' AND pr.item_type = 'pr' AND pr.priority IS NULL
           AND iss.external_id = ANY(pr.linked_keys) AND iss.priority IN ('P0', 'P1')""")
    if labelled:
        fids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
        await conn.executemany("""
            INSERT INTO work_item_allocations (work_item_id, feature_id, org_id, share)
            SELECT w.id, $2, $3, 1 FROM work_items w
             WHERE w.item_type = 'pr' AND $1 = ANY(w.linked_keys)
               AND NOT EXISTS (SELECT 1 FROM work_item_allocations a WHERE a.work_item_id = w.id)
            ON CONFLICT DO NOTHING""", [(k, fids[f], org_id) for k, f in labelled if f in fids])


# ------------------------------------------------------------------------------------------ Zendesk tickets
async def _tickets(conn, org_id: str, envs: list[dict], manifest: Manifest, index: AccountIndex,
                   failed: dict[str, str]) -> None:
    org_to_acct = {ext: acct for (sysname, ext), acct in index.by_identity.items() if sysname == "zendesk"}
    fids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
    rows, labels = [], {}
    for e in envs:
        try:
            t = N.ticket_row(e["payload"], manifest, org_to_acct)
        except Exception as ex:  # noqa: BLE001
            failed[e["idempotency_key"]] = f"zendesk normalize: {ex}"[:500]
            continue
        fid = fids.get(t.feature_key) if t.feature_key else None
        rows.append((str(uuid.uuid5(TICKET_NS, f"{org_id}|{t.external_id}")), org_id, t.external_id, t.account_id, fid,
                     "manifest" if fid else None, 1 if fid else None, t.priority, t.created_at, t.handle_minutes,
                     t.org_ref, t.estimated))
        if not fid:
            labels[t.external_id] = f"{t.external_id} · {t.subject}"[:160]
    await conn.executemany("""
        INSERT INTO support_tickets (id, org_id, external_id, account_id, feature_id, mapping_source, mapping_confidence,
                                     priority, created_at, handle_minutes, external_org_ref, handle_time_estimated)
        VALUES ($1, $2, $3, $4, $5, $6::mapping_source, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (org_id, external_id) DO UPDATE SET account_id = EXCLUDED.account_id,
            priority = EXCLUDED.priority, handle_minutes = EXCLUDED.handle_minutes,
            external_org_ref = EXCLUDED.external_org_ref, handle_time_estimated = EXCLUDED.handle_time_estimated,
            feature_id = CASE WHEN support_tickets.mapping_source = 'human' THEN support_tickets.feature_id
                              ELSE EXCLUDED.feature_id END,
            mapping_source = CASE WHEN support_tickets.mapping_source = 'human' THEN support_tickets.mapping_source
                                  ELSE EXCLUDED.mapping_source END""", rows)
    if labels:
        ids = {r["external_id"]: r["id"] for r in await conn.fetch(
            "SELECT id, external_id FROM support_tickets WHERE external_id = ANY($1) AND feature_id IS NULL",
            list(labels))}
        await conn.executemany("""
            INSERT INTO mapping_suggestions (org_id, subject_type, subject_id, subject_label, suggested_feature_id,
                                             confidence, source, rationale)
            VALUES ($1, 'support_ticket', $2, $3, NULL, 0, 'heuristic',
                    'No Zendesk tag matched a feature key or support_tags in features.yaml')
            ON CONFLICT (org_id, subject_type, subject_id) DO NOTHING""",
            [(org_id, ids[x], lbl) for x, lbl in labels.items() if x in ids])


# ------------------------------------------------------------------------------------------ month rebuilds
async def _rebuild_usage_month(conn, org_id: str, month: date, manifest: Manifest, index: AccountIndex,
                               st: Stats) -> None:
    m = month.isoformat()
    usage = await _latest(conn, "posthog_usage", "payload->>'month' = $2", m)
    activity = await _latest(conn, "posthog_activity", "payload->>'month' = $2", m)
    groups = {p["group_key"] for p in usage} | {p["group_key"] for p in activity}
    g2a = {g: index.by_identity.get(("posthog", g)) for g in groups}
    unres = await unresolved_account(conn, org_id)
    um = N.usage_month(month, usage, activity, manifest, g2a, unres)
    fids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
    await conn.execute("DELETE FROM usage_signals WHERE period_month = $1", month)
    await conn.execute("DELETE FROM account_activity_monthly WHERE period_month = $1", month)
    await conn.executemany("""INSERT INTO usage_signals (org_id, period_month, account_id, feature_id, events,
                                                          active_days, active_users) VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                           [(org_id, month, a, fids[fk], v["events"], v["active_days"], v["active_users"])
                            for (a, fk), v in um.usage.items() if fk in fids])
    await conn.executemany("""INSERT INTO account_activity_monthly (org_id, period_month, account_id, active_days)
                              VALUES ($1, $2, $3, $4)""", [(org_id, month, a, d) for a, d in um.activity.items()])
    st.months_rebuilt.append(f"usage:{m}")
    st.unknown_events += len(um.unknown_events)


async def _rebuild_cost_month(conn, org_id: str, month: date, manifest: Manifest, st: Stats) -> None:
    rows = await _latest(conn, "aws_cost", "payload->>'month' = $2", month.isoformat())
    fids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
    await conn.execute("DELETE FROM cost_events WHERE source = 'aws_cost_explorer' AND period_month = $1", month)
    await conn.executemany("""INSERT INTO cost_events (org_id, period_month, feature_id, cost_type, source, amount_usd, unit)
                              VALUES ($1, $2, $3, $4::cost_type, 'aws_cost_explorer', $5, 'usd')""",
                           [(org_id, month, fids.get(fk) if fk else None, ct, amt)
                            for (fk, ct), amt in N.cost_month(rows, manifest).items() if amt])
    st.months_rebuilt.append(f"costs:{month.isoformat()}")


# ------------------------------------------------------------------------------------------ driver
async def process_inbox(conn, org_id: str, manifest: Manifest, fx: dict[str, float], limit: int = 50_000) -> Stats:
    st = Stats()
    pending = await conn.fetch("""
        SELECT idempotency_key, entity_type, external_id, payload, attempts FROM ingest_inbox
         WHERE processed_at IS NULL AND dead_lettered_at IS NULL ORDER BY received_at LIMIT $1""", limit)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in pending:
        by_type[r["entity_type"]].append(dict(r))
    if not pending:
        return st
    index = await load_index(conn)
    failed: dict[str, str] = {}
    done: list[str] = []
    accounts_changed = False

    async def guarded(name: str, envs: list[dict], fn) -> None:
        nonlocal failed
        if not envs:
            return
        try:
            async with conn.transaction():   # savepoint: a SQL failure rolls back this handler only
                await fn()
        except Exception as ex:  # noqa: BLE001
            log.exception("processor %s failed", name)
            for e in envs:
                failed.setdefault(e["idempotency_key"], f"{name}: {ex}"[:500])
        done.extend(e["idempotency_key"] for e in envs if e["idempotency_key"] not in failed)

    stripe_envs = by_type["stripe_customer"] + by_type["stripe_subscription"] + by_type["stripe_invoice"]

    async def do_stripe() -> None:
        nonlocal accounts_changed, index
        accounts_changed = await _stripe(conn, org_id, stripe_envs, fx, st, failed)
        index = await load_index(conn)
    await guarded("stripe", stripe_envs, do_stripe)

    # stitching: pending orgs/groups, or ALL of them when new accounts appeared (late re-stitch)
    for kind, entity in (("zendesk", "zendesk_org"), ("posthog", "posthog_group")):
        envs = by_type[entity]
        if accounts_changed:
            payloads = await _latest(conn, entity, "true")
        else:
            payloads = [e["payload"] for e in envs]

        async def do_stitch(kind=kind, payloads=payloads) -> None:
            await _stitch(conn, org_id, index, kind, payloads, st)
        if payloads:
            await guarded(f"stitch:{kind}", envs or [{"idempotency_key": f"_restitch_{kind}"}], do_stitch)
    if accounts_changed:
        await conn.execute("""UPDATE support_tickets t SET account_id = i.account_id FROM account_identities i
                               WHERE t.account_id IS NULL AND i.system = 'zendesk' AND i.external_id = t.external_org_ref""")

    await guarded("jira", by_type["jira_issue"], lambda: _jira(conn, org_id, by_type["jira_issue"], manifest, failed))
    await guarded("zendesk_tickets", by_type["zendesk_ticket"],
                  lambda: _tickets(conn, org_id, by_type["zendesk_ticket"], manifest, index, failed))

    usage_envs = by_type["posthog_usage"] + by_type["posthog_activity"]
    months = {date.fromisoformat(e["payload"]["month"]) for e in usage_envs if e["payload"].get("month")}
    if accounts_changed:
        months |= {r[0] for r in await conn.fetch(
            "SELECT DISTINCT (payload->>'month')::date FROM ingest_inbox WHERE entity_type = 'posthog_usage'") if r[0]}
    for e in usage_envs:
        if not e["payload"].get("month"):
            failed[e["idempotency_key"]] = "posthog payload has no month"

    async def do_usage() -> None:
        for m in sorted(months):
            await _rebuild_usage_month(conn, org_id, m, manifest, index, st)
    if months:
        await guarded("posthog_usage", usage_envs or [{"idempotency_key": "_usage_rebuild"}], do_usage)

    cost_envs = by_type["aws_cost"]
    cmonths = {date.fromisoformat(e["payload"]["month"]) for e in cost_envs if e["payload"].get("month")}

    async def do_costs() -> None:
        for m in sorted(cmonths):
            await _rebuild_cost_month(conn, org_id, m, manifest, st)
    await guarded("aws_cost", cost_envs, do_costs)

    known = set(ORDER)
    for t, envs in by_type.items():
        if t not in known:
            for e in envs:
                failed[e["idempotency_key"]] = f"no processor for entity type {t!r}"

    real_done = [k for k in done if not k.startswith("_") and k not in failed]
    await conn.execute("UPDATE ingest_inbox SET processed_at = now() WHERE idempotency_key = ANY($1)", real_done)
    real_failed = {k: v for k, v in failed.items() if not k.startswith("_")}
    if real_failed:
        await conn.executemany("""
            UPDATE ingest_inbox SET attempts = attempts + 1, last_error = $2,
                   dead_lettered_at = CASE WHEN attempts + 1 >= $3 THEN now() END
             WHERE idempotency_key = $1""", [(k, v, MAX_ATTEMPTS) for k, v in real_failed.items()])
        st.dead_lettered = await conn.fetchval(
            "SELECT count(*) FROM ingest_inbox WHERE idempotency_key = ANY($1) AND dead_lettered_at IS NOT NULL",
            list(real_failed))
    st.processed = len(real_done)
    st.failed = len(real_failed)
    return st
