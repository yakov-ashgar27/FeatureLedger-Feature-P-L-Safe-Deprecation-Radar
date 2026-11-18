"""PII & authorship scrubbers: ALLOWLISTS per source (spec §3.2).

Nothing leaves a connector unless it is listed here. Dropped by construction: assignees, reporters,
creators, watchers, requesters, submitters, collaborators, person ids, emails (only the domain is kept,
for identity stitching), contact names, ticket descriptions and comment threads, issue summaries and
descriptions, ticket subjects and PR titles. (Phase 3: the classifier receives that text in memory only,
through a TextVault, and stores nothing but the embedding.) A new field added by a vendor is dropped until someone adds it here on purpose.
"""
from __future__ import annotations

from typing import Any

FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com",
             "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com"}
SUBJECT_MAX = 120


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    d = email.rsplit("@", 1)[1].strip().lower()
    return None if not d or d in FREE_MAIL else d


def norm_domain(d: str | None) -> str | None:
    if not d:
        return None
    d = d.strip().lower().removeprefix("http://").removeprefix("https://").removeprefix("www.").split("/")[0]
    return d or None


def _name(obj: Any) -> str | None:
    return obj.get("name") if isinstance(obj, dict) else None


# ----------------------------------------------------------------------------- Jira
def scrub_jira_issue(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields") or {}
    parent = f.get("parent") or {}
    return {
        "key": issue["key"],
        "project": (f.get("project") or {}).get("key"),
        "issuetype": _name(f.get("issuetype")),
        "priority": _name(f.get("priority")),
        "labels": sorted(set(f.get("labels") or [])),
        "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key"),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "resolved": f.get("resolutiondate"),
        "parent_key": parent.get("key"),
        "components": sorted({c.get("name") for c in f.get("components") or [] if c.get("name")}),
    }


# ----------------------------------------------------------------------------- PostHog
def scrub_posthog_group(g: dict[str, Any]) -> dict[str, Any]:
    props = g.get("group_properties") or {}
    return {"key": str(g["group_key"]), "name": props.get("name"), "domain": norm_domain(props.get("domain"))}


# ----------------------------------------------------------------------------- Stripe
def scrub_stripe_customer(c: dict[str, Any]) -> dict[str, Any]:
    md = c.get("metadata") or {}
    return {
        "id": c["id"],
        # B2B: the account name comes from company metadata, never from a person's name field
        "name": md.get("company_name") or c.get("description") or c["id"],
        "email_domain": email_domain(c.get("email")),
        "currency": (c.get("currency") or "usd").lower(),
        "created": c.get("created"),
        "deleted": bool(c.get("deleted")),
        "metadata": {k: md[k] for k in ("tier", "health", "csm_email", "contract_renewal", "domain") if k in md},
    }


def scrub_stripe_subscription(s: dict[str, Any]) -> dict[str, Any]:
    items = []
    for it in (s.get("items") or {}).get("data", []):
        p = it.get("price") or {}
        rec = p.get("recurring") or {}
        items.append({
            "quantity": int(it.get("quantity") or 1),
            "unit_amount": int(p.get("unit_amount") or 0),
            "currency": (p.get("currency") or "usd").lower(),
            "interval": rec.get("interval"),
            "interval_count": int(rec.get("interval_count") or 1),
            "tier": (p.get("metadata") or {}).get("tier"),
            "current_period_end": it.get("current_period_end"),
        })
    return {
        "id": s["id"], "customer": s["customer"], "status": s.get("status"), "created": s.get("created"),
        "current_period_end": s.get("current_period_end"), "cancel_at": s.get("cancel_at"),
        "tier": (s.get("metadata") or {}).get("tier"), "items": items,
    }


def scrub_stripe_invoice(inv: dict[str, Any]) -> dict[str, Any]:
    lines = []
    for ln in (inv.get("lines") or {}).get("data", []):
        per = ln.get("period") or {}
        lines.append({"amount": int(ln.get("amount") or 0), "start": per.get("start"), "end": per.get("end"),
                      "subscription": ln.get("subscription")})
    return {"id": inv["id"], "customer": inv["customer"], "status": inv.get("status"),
            "currency": (inv.get("currency") or "usd").lower(), "created": inv.get("created"), "lines": lines}


# ----------------------------------------------------------------------------- Zendesk
def scrub_zendesk_ticket(t: dict[str, Any], metric_set: dict[str, Any] | None,
                         time_field_id: int | None) -> dict[str, Any]:
    time_spent = None
    if time_field_id is not None:
        for cf in t.get("custom_fields") or []:
            if cf.get("id") == time_field_id and cf.get("value") not in (None, ""):
                time_spent = int(float(cf["value"]))
    return {
        "id": t["id"],
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "organization_id": t.get("organization_id"),
        "tags": sorted(set(t.get("tags") or [])),
        "status": t.get("status"),
        "priority": t.get("priority"),
        "time_spent_seconds": time_spent,
        "replies": (metric_set or {}).get("replies"),
    }


def scrub_zendesk_org(o: dict[str, Any]) -> dict[str, Any]:
    return {"id": o["id"], "name": o.get("name"),
            "domains": sorted({d for d in (norm_domain(x) for x in o.get("domain_names") or []) if d})}
