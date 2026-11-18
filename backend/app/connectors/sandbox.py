"""SANDBOX_MODE: realistic, vendor-shaped API responses served from memory.

The sandbox APIs are built from the deterministic seed company (Northwind Cloud), so a fresh
`docker compose up` exercises the *real* connector code (pagination, cursors, scrubbing, retries,
stitching, processors) end to end, and the resulting ledger matches the Phase 1 demo exactly.

The responses deliberately include the fields a real API returns and the scrubbers must drop
(assignees, reporters, requester ids, emails, contact names, ticket descriptions) so the privacy
guarantees are tested on every sandbox sync, not only in unit tests.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .base import HttpResponse, add_months

SANDBOX_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
SANDBOX_TIME_FIELD_ID = 900
EXTRA_UNRESOLVED_GROUPS = [("grp_trial_bluefin", "Bluefin Trial", "bluefin-trial.io"),
                           ("grp_trial_kestrel", "Kestrel (trial)", "kestrel.dev"),
                           ("grp_personal_gmail", "Personal workspace", "gmail.com")]

Router = Callable[[str, str, dict[str, Any], Any], HttpResponse]


def _ts(d: date | datetime) -> int:
    if isinstance(d, datetime):
        return int(d.timestamp())
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _jira_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _ok(body: Any) -> HttpResponse:
    return HttpResponse(200, {"content-type": "application/json"}, body)


class SandboxTransport:
    """A Transport whose 'network' is a router function. `fail_first` makes the first N requests
    answer 429 with Retry-After: 0, to exercise the retry path in tests and demos."""

    def __init__(self, router: Router, fail_first: int = 0):
        self.router = router
        self.fail_first = fail_first
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, *, params=None, json_body=None, headers=None) -> HttpResponse:
        u = urlparse(url)
        q: dict[str, Any] = {k: (v if len(v) > 1 else v[0]) for k, v in parse_qs(u.query).items()}
        q.update(params or {})
        self.calls.append((method, u.path, q))
        if self.fail_first > 0:
            self.fail_first -= 1
            return HttpResponse(429, {"Retry-After": "0"}, {"error": "rate limited (sandbox)"})
        return self.router(method, u.path, q, json_body)


@lru_cache(maxsize=2)
def _world(manifest_text: str) -> dict[str, Any]:
    """Everything the sandbox APIs serve, derived once from the seed company."""
    from ..seed.generate import MONTHS, account_domain, build, pr_issue_key, stripe_ref
    seed = build(manifest_text)
    ds, ex = seed.dataset, seed.extras
    feats = {f.id: f for f in ds.features}
    spec = seed.manifest.by_key()
    accounts = ex["accounts"]
    by_id = {a.id: a for a in accounts}
    group_key = {a.id: "grp_" + re.sub(r"[^a-z0-9]+", "_", a.name.lower()).strip("_") for a in accounts}
    org_id = {a.id: 360_000_000_000 + i for i, a in enumerate(accounts)}
    return {"seed": seed, "MONTHS": MONTHS, "feats": feats, "spec": spec, "accounts": accounts, "by_id": by_id,
            "group_key": group_key, "org_id": org_id, "domain": account_domain, "stripe_ref": stripe_ref,
            "pr_issue_key": pr_issue_key, "csm": ex["csm"], "tickets": ex["ticket_rows"], "prs": ex["wi_rows"]}


def _page(items: list[Any], limit: int, after_id: str | None, key: str = "id") -> tuple[list[Any], bool]:
    start = 0
    if after_id:
        idx = next((i for i, x in enumerate(items) if str(x[key]) == str(after_id)), None)
        start = (idx + 1) if idx is not None else len(items)
    chunk = items[start:start + limit]
    return chunk, start + limit < len(items)


# ---------------------------------------------------------------------------------------- Stripe
def stripe_router(manifest_text: str) -> Router:
    w = _world(manifest_text)
    customers, subs, invoices = [], [], []
    for a in w["accounts"]:
        ref = w["stripe_ref"](a.id)
        dom = w["domain"](a.name)
        md = {"company_name": a.name, "tier": a.plan_tier, "health": a.health}
        if w["csm"].get(a.id):
            md["csm_email"] = w["csm"][a.id]
        if a.renewal_date:
            md["contract_renewal"] = a.renewal_date.isoformat()
        customers.append({"id": ref, "object": "customer", "email": f"jane.doe@{dom}", "name": "Jane Doe",
                          "phone": "+1 555 0100", "address": {"line1": "1 Main St"}, "currency": "usd",
                          "created": _ts(date(2024, 1, 1)), "description": None, "metadata": md})
        if a.arr_usd <= 0:
            continue
        unit = int(round(a.arr_usd * 100 / 12))
        sub_id = "sub_" + ref[4:]
        subs.append({"id": sub_id, "object": "subscription", "customer": ref, "status": "active",
                     "created": _ts(date(2024, 1, 1)), "current_period_end": _ts(date(2026, 12, 1)),
                     "metadata": {}, "items": {"data": [{
                         "quantity": 1, "current_period_end": _ts(date(2026, 12, 1)),
                         "price": {"unit_amount": unit, "currency": "usd", "metadata": {"tier": a.plan_tier},
                                   "recurring": {"interval": "month", "interval_count": 1}}}]}})
        for m in w["MONTHS"]:
            invoices.append({"id": f"in_{ref[4:]}_{m:%Y%m}", "object": "invoice", "customer": ref, "status": "paid",
                             "currency": "usd", "created": _ts(m), "customer_email": f"jane.doe@{dom}",
                             "lines": {"data": [{"amount": unit, "subscription": sub_id,
                                                 "period": {"start": _ts(m), "end": _ts(add_months(m, 1))}}]}})

    def route(method: str, path: str, q: dict[str, Any], body: Any) -> HttpResponse:
        limit = int(q.get("limit", 100))
        if path == "/v1/customers":
            data, more = _page(customers, limit, q.get("starting_after"))
        elif path == "/v1/subscriptions":
            data, more = _page(subs, limit, q.get("starting_after"))
        elif path == "/v1/invoices":
            since = int(q.get("created[gte]", 0))
            data, more = _page([i for i in invoices if i["created"] >= since], limit, q.get("starting_after"))
        elif path == "/v1/events":
            data, more = [], False   # no new activity in the frozen sandbox month
        else:
            return HttpResponse(404, {}, {"error": {"message": f"no sandbox route {path}"}})
        return _ok({"object": "list", "data": data, "has_more": more})
    return route


# ---------------------------------------------------------------------------------------- PostHog
def posthog_router(manifest_text: str) -> Router:
    w = _world(manifest_text)
    ds = w["seed"].dataset
    groups = [{"group_type_index": 0, "group_key": w["group_key"][a.id], "created_at": "2024-01-01T00:00:00Z",
               "group_properties": {"name": a.name, "domain": w["domain"](a.name), "owner_email": f"ceo@{w['domain'](a.name)}"}}
              for a in w["accounts"]]
    groups += [{"group_type_index": 0, "group_key": k, "created_at": "2026-10-01T00:00:00Z",
                "group_properties": {"name": n, "domain": d}} for k, n, d in EXTRA_UNRESOLVED_GROUPS]
    event_of = {fid: w["spec"][f.key].events[0] for fid, f in w["feats"].items() if w["spec"][f.key].events}
    usage: dict[date, list[list[Any]]] = {}
    for u in ds.usage:
        if u.feature_id in event_of:
            usage.setdefault(u.period_month, []).append(
                [w["group_key"][u.account_id], event_of[u.feature_id], u.active_days, u.active_users, u.events])
    last = w["MONTHS"][-1]
    for k, _, _ in EXTRA_UNRESOLVED_GROUPS:
        usage.setdefault(last, []).append([k, "doc_edited", 6, 2, 340])
    activity: dict[date, list[list[Any]]] = {}
    for (m, aid), days in ds.account_active_days.items():
        activity.setdefault(m, []).append([w["group_key"][aid], days])
    for k, _, _ in EXTRA_UNRESOLVED_GROUPS:
        activity.setdefault(last, []).append([k, 6])

    def route(method: str, path: str, q: dict[str, Any], body: Any) -> HttpResponse:
        if path.endswith("/groups/"):
            limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
            chunk = groups[offset:offset + limit]
            nxt = (f"{path}?group_type_index={q.get('group_type_index', 0)}&limit={limit}&offset={offset + limit}"
                   if offset + limit < len(groups) else None)
            return _ok({"results": chunk, "next": nxt})
        if path.endswith("/query/"):
            qq = (body or {}).get("query", {})
            m = date.fromisoformat(qq.get("values", {}).get("start"))
            if "person_id" in qq.get("query", ""):
                return _ok({"columns": ["group_key", "event", "active_days", "active_users", "events"],
                            "results": usage.get(m, [])})
            return _ok({"columns": ["group_key", "active_days"], "results": activity.get(m, [])})
        return HttpResponse(404, {}, {"detail": f"no sandbox route {path}"})
    return route


# ---------------------------------------------------------------------------------------- Zendesk
def zendesk_router(manifest_text: str) -> Router:
    w = _world(manifest_text)
    orgs = [{"id": w["org_id"][a.id], "name": a.name, "domain_names": [w["domain"](a.name)],
             "updated_at": "2026-01-01T00:00:00Z", "notes": "VIP contact: jane.doe", "details": "HQ address"}
            for a in w["accounts"]]
    tickets, metric_sets = [], []
    for t in sorted(w["tickets"], key=lambda t: (t["solved"], t["external_id"])):
        tid = int(t["external_id"].split("-")[1])
        f = w["feats"].get(t["feature"]) if t["feature"] else None
        tickets.append({
            "id": tid, "created_at": t["created"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": t["solved"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "organization_id": w["org_id"].get(t["account"]), "tags": [f.key] if f else ["question"],
            "subject": f"Problem with {f.name}" if f else "General question",
            "description": "Hi, this is Jane Doe (jane.doe@example.com), my export is broken...",
            "requester_id": 1001, "submitter_id": 1001, "assignee_id": 2002, "collaborator_ids": [3003],
            "status": "solved", "priority": "normal",
            "custom_fields": [{"id": SANDBOX_TIME_FIELD_ID, "value": t["minutes"] * 60}]})
        metric_sets.append({"ticket_id": tid, "replies": 2, "full_resolution_time_in_minutes": {"business": 120}})
    page = 500

    def route(method: str, path: str, q: dict[str, Any], body: Any) -> HttpResponse:
        if path == "/api/v2/organizations":
            size = int(q.get("page[size]", 100))
            start = int(q.get("page[after]") or 0)
            chunk = orgs[start:start + size]
            more = start + size < len(orgs)
            return _ok({"organizations": chunk, "meta": {"has_more": more, "after_cursor": str(start + size)}})
        if path == "/api/v2/incremental/tickets/cursor.json":
            if "cursor" in q:
                start = int(str(q["cursor"]).removeprefix("c"))
            else:
                since = int(q.get("start_time", 0))
                start = next((i for i, t in enumerate(tickets)
                              if _ts(datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00"))) >= since),
                             len(tickets))
            chunk = tickets[start:start + page]
            ids = {t["id"] for t in chunk}
            end = start + len(chunk)
            return _ok({"tickets": chunk, "metric_sets": [m for m in metric_sets if m["ticket_id"] in ids],
                        "after_cursor": f"c{end}", "end_of_stream": end >= len(tickets)})
        return HttpResponse(404, {}, {"error": f"no sandbox route {path}"})
    return route


# ---------------------------------------------------------------------------------------- Jira
def jira_router(manifest_text: str) -> Router:
    w = _world(manifest_text)
    issues = []
    for pr in sorted(w["prs"], key=lambda p: (p["merged"], p["external_id"])):
        kind, prio = pr["kind"], pr["priority"]
        itype = {"bug": "Bug", "hotfix": "Bug", "feature": "Story", "chore": "Task"}.get(kind, "Task")
        pname = {"P0": "Highest", "P1": "High"}.get(prio or "", "Medium")
        issues.append({"key": w["pr_issue_key"](pr["external_id"]), "id": pr["external_id"].rsplit("#", 1)[1],
                       "fields": {
                           "issuetype": {"name": itype}, "priority": {"name": pname}, "labels": [],
                           "status": {"name": "Done", "statusCategory": {"key": "done"}},
                           "created": _jira_ts(pr["opened"]), "updated": _jira_ts(pr["merged"]),
                           "resolutiondate": _jira_ts(pr["merged"]), "project": {"key": "NW"},
                           "summary": "Customer Jane Doe reported...", "description": {"type": "doc"},
                           "assignee": {"displayName": "Dev Person", "emailAddress": "dev@northwind.io"},
                           "reporter": {"displayName": "PM Person"}, "creator": {"displayName": "PM Person"},
                           "watches": {"watchCount": 3}, "comment": {"comments": [{"body": "looking"}]}}})

    def route(method: str, path: str, q: dict[str, Any], body: Any) -> HttpResponse:
        if path != "/rest/api/3/search/jql":
            return HttpResponse(404, {}, {"errorMessages": [f"no sandbox route {path}"]})
        jql = (body or {}).get("jql", "")
        m = re.search(r'updated >= "([^"]+)"', jql)
        since = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) if m else None
        sel = [i for i in issues if not since or
               datetime.strptime(i["fields"]["updated"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) >= since]
        start = int((body or {}).get("nextPageToken") or 0)
        size = int((body or {}).get("maxResults", 50))
        chunk = sel[start:start + size]
        more = start + size < len(sel)
        return _ok({"issues": chunk, "nextPageToken": str(start + size) if more else None, "isLast": not more})
    return route


# ---------------------------------------------------------------------------------------- AWS Cost Explorer
class SandboxCostExplorer:
    def __init__(self, manifest_text: str, page_size: int = 20):
        w = _world(manifest_text)
        self.page_size = page_size
        self.by_month: dict[str, list[dict[str, Any]]] = {}
        for c in w["seed"].dataset.cost_lines:
            key = "feature$" + (w["feats"][c.feature_id].key if c.feature_id else "")
            if c.cost_type == "llm":
                parts = [("Amazon Bedrock", c.amount_usd)]
            else:
                ec2 = round(c.amount_usd * 0.7, 2)
                parts = [("Amazon Elastic Compute Cloud - Compute", ec2),
                         ("Amazon Relational Database Service", round(c.amount_usd - ec2, 2))]
            for svc, amt in parts:
                self.by_month.setdefault(c.period_month.isoformat(), []).append(
                    {"Keys": [key, svc], "Metrics": {"UnblendedCost": {"Amount": f"{amt:.2f}", "Unit": "USD"}}})

    async def get_cost_and_usage(self, **kw: Any) -> dict[str, Any]:
        start = kw["TimePeriod"]["Start"]
        groups = self.by_month.get(start, [])
        offset = int(kw.get("NextPageToken") or 0)
        chunk = groups[offset:offset + self.page_size]
        out: dict[str, Any] = {"ResultsByTime": [{"TimePeriod": kw["TimePeriod"], "Groups": chunk, "Estimated": False}]}
        if offset + self.page_size < len(groups):
            out["NextPageToken"] = str(offset + self.page_size)
        return out


def sandbox_clock() -> datetime:
    return SANDBOX_NOW

