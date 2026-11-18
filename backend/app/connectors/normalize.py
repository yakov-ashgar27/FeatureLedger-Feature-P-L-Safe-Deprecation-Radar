"""Pure normalization: scrubbed envelope payloads -> canonical FeatureLedger rows.

Used by the database processors and by the in-memory round-trip test (sandbox APIs -> connectors ->
normalizers -> P&L engine must reproduce the seed company's ledger exactly).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..core.manifest import Manifest
from .jira import kind_of, priority_of
from .stitching import AccountIndex, Match, resolve
from .stripe_billing import account_state, monthly_mrr_from_invoices
from .zendesk import handle_minutes


# ------------------------------------------------------------------------- feature resolution
def feature_for_event(event: str, manifest: Manifest) -> str | None:
    return manifest.feature_for_event(event)


def feature_for_tags(tags: list[str], manifest: Manifest) -> str | None:
    """Zendesk tags: a manifest `support_tags` entry, or a tag equal to a feature key (convention)."""
    keys = manifest.by_key()
    for t in tags:
        for f in manifest.features:
            if t in f.support_tags:
                return f.key
    for t in tags:
        if t in keys and not keys[t].shared_overhead:
            return t
    return None


def feature_for_cost_tag(tag: str | None, manifest: Manifest) -> str | None:
    if not tag:
        return None
    if tag in manifest.by_key():
        return tag
    for f in manifest.features:
        if tag in f.cost_tags:
            return f.key
    return None


def feature_for_jira(issue: dict[str, Any], manifest: Manifest) -> str | None:
    labels = set(issue.get("labels") or [])
    for f in manifest.features:
        if labels & set(f.jira_labels) or (issue.get("parent_key") and issue["parent_key"] in f.jira_epics):
            return f.key
    return None


# ------------------------------------------------------------------------- Stripe
@dataclass
class StripeAccount:
    external_ref: str
    name: str
    email_domain: str | None
    plan_tier: str
    arr_usd: float
    renewal_date: date | None
    health: str
    csm_email: str | None
    snapshots: dict[date, float] = field(default_factory=dict)   # month -> ARR


def stripe_accounts(customers: list[dict], subs: list[dict], invoices: list[dict],
                    fx: dict[str, float]) -> list[StripeAccount]:
    subs_by: dict[str, list[dict]] = defaultdict(list)
    inv_by: dict[str, list[dict]] = defaultdict(list)
    for s in subs:
        subs_by[s["customer"]].append(s)
    for i in invoices:
        inv_by[i["customer"]].append(i)
    out = []
    for c in customers:
        if c.get("deleted"):
            continue
        st = account_state(c, subs_by.get(c["id"], []), fx)
        mrr = monthly_mrr_from_invoices(inv_by.get(c["id"], []), fx)
        out.append(StripeAccount(
            c["id"], c["name"], c.get("email_domain") or (c.get("metadata") or {}).get("domain"), st["plan_tier"],
            st["arr_usd"], date.fromisoformat(st["renewal_date"]) if st["renewal_date"] else None, st["health"],
            st["csm_email"], {m: round(v * 12, 2) for m, v in mrr.items()}))
    return out


# ------------------------------------------------------------------------- PostHog
@dataclass
class UsageMonth:
    month: date
    usage: dict[tuple[str, str], dict[str, int]]     # (account_id, feature_key) -> events/active_days/active_users
    activity: dict[str, int]                         # account_id -> distinct active days
    unknown_events: set[str]
    unresolved_groups: set[str]


def group_matches(groups: list[dict], index: AccountIndex) -> dict[str, Match]:
    return {g["key"]: resolve(index, "posthog", g["key"], [g["domain"]] if g.get("domain") else []) for g in groups}


def usage_month(month: date, usage_rows: list[dict], activity_rows: list[dict], manifest: Manifest,
                group_to_account: dict[str, str | None], unresolved_account: str) -> UsageMonth:
    agg: dict[tuple[str, str], dict[str, int]] = {}
    unknown, unresolved = set(), set()
    for r in usage_rows:
        fk = manifest.feature_for_event(r["event"])
        if fk is None:
            unknown.add(r["event"])
            continue
        acct = group_to_account.get(r["group_key"])
        if acct is None:
            unresolved.add(r["group_key"])
            acct = unresolved_account
        a = agg.setdefault((acct, fk), {"events": 0, "active_days": 0, "active_users": 0})
        a["events"] += int(r["events"])
        a["active_days"] = max(a["active_days"], int(r["active_days"]))     # distinct days do not add up
        a["active_users"] = max(a["active_users"], int(r["active_users"]))
    act: dict[str, int] = {}
    for r in activity_rows:
        acct = group_to_account.get(r["group_key"]) or unresolved_account
        act[acct] = max(act.get(acct, 0), int(r["active_days"]))
    for (acct, _), a in agg.items():
        act[acct] = max(act.get(acct, 0), a["active_days"])
    return UsageMonth(month, agg, {k: min(v, 31) for k, v in act.items()}, unknown, unresolved)


# ------------------------------------------------------------------------- Zendesk
@dataclass
class TicketRow:
    external_id: str
    org_ref: str | None
    account_id: str | None
    feature_key: str | None
    created_at: datetime
    handle_minutes: int
    estimated: bool
    priority: str | None
    subject: str


def ticket_row(t: dict[str, Any], manifest: Manifest, org_to_account: dict[str, str | None]) -> TicketRow:
    minutes, est = handle_minutes(t)
    org = str(t["organization_id"]) if t.get("organization_id") is not None else None
    return TicketRow(f"ZD-{t['id']}", org, org_to_account.get(org) if org else None,
                     feature_for_tags(t.get("tags") or [], manifest),
                     datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")), minutes, est,
                     t.get("priority"), t.get("subject") or "")


def org_matches(orgs: list[dict], index: AccountIndex) -> dict[str, Match]:
    return {str(o["id"]): resolve(index, "zendesk", str(o["id"]), o.get("domains") or []) for o in orgs}


# ------------------------------------------------------------------------- AWS
def cost_month(rows: list[dict], manifest: Manifest) -> dict[tuple[str | None, str], float]:
    out: dict[tuple[str | None, str], float] = defaultdict(float)
    for r in rows:
        out[(feature_for_cost_tag(r.get("feature_tag"), manifest), r["cost_type"])] += float(r["amount_usd"])
    return {k: round(v, 2) for k, v in out.items()}


# ------------------------------------------------------------------------- Jira
def jira_row(issue: dict[str, Any], manifest: Manifest) -> dict[str, Any]:
    return {"key": issue["key"], "kind": kind_of(issue), "priority": priority_of(issue),
            "created": issue.get("created"), "resolved": issue.get("resolved"),
            "labels": issue.get("labels") or [], "feature_key": feature_for_jira(issue, manifest)}
