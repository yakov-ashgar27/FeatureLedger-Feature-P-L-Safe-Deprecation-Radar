"""CSV imports: the bridge until live Stripe / PostHog / Zendesk / AWS connectors are installed.

Pure parsing + validation (no I/O). Every row is checked; the import is all-or-nothing, and the
caller gets line-numbered errors it can show to the user. Templates live in /templates.

accounts.csv  external_ref,name,plan_tier,arr_usd,renewal_date,health,csm_email
usage.csv     account_ref,feature,active_days,active_users,events[,account_active_days]
tickets.csv   external_id,account_ref,feature,created_at,handle_minutes
costs.csv     feature,cost_type,amount_usd
`feature` may be a feature key from features.yaml, an analytics event name, or (tickets) a
support tag. Leave it empty in costs.csv for untagged spend.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from ..core.manifest import Manifest

TIERS = {"free", "starter", "pro", "enterprise"}
HEALTH = {"green", "yellow", "red"}
COST_TYPES = {"infra", "llm"}
MAX_ROWS = 200_000

SCHEMAS: dict[str, list[str]] = {
    "accounts": ["external_ref", "name", "plan_tier", "arr_usd"],
    "usage": ["account_ref", "feature", "active_days", "active_users"],
    "tickets": ["external_id", "feature", "created_at", "handle_minutes"],
    "costs": ["feature", "cost_type", "amount_usd"],
}


class ImportError_(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__(f"{len(errors)} error(s): " + "; ".join(errors[:5]))
        self.errors = errors


@dataclass
class Parsed:
    kind: str
    rows: list[dict[str, Any]]


def resolve_feature(value: str, manifest: Manifest, *, allow_tags: bool = False) -> str | None:
    v = value.strip()
    if not v:
        return None
    if v in manifest.by_key():
        return v
    key = manifest.feature_for_event(v)
    if key:
        return key
    if allow_tags:
        for f in manifest.features:
            if v in f.support_tags:
                return f.key
    return None


def _num(raw: str, field: str, line: int, errors: list[str], *, integer: bool = False,
         lo: float = 0, hi: float = float("inf")) -> float | int | None:
    try:
        v = int(raw) if integer else float(raw.replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        errors.append(f"line {line}: '{field}' must be a {'whole number' if integer else 'number'} (got {raw!r})")
        return None
    if not lo <= v <= hi:
        errors.append(f"line {line}: '{field}' must be between {lo:g} and {hi:g} (got {raw})")
        return None
    return v


def parse_csv(kind: str, text: str, manifest: Manifest) -> Parsed:
    if kind not in SCHEMAS:
        raise ImportError_([f"unknown import kind {kind!r}; expected one of {sorted(SCHEMAS)}"])
    text = text.lstrip("﻿")  # Excel UTF-8 BOM
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in SCHEMAS[kind] if c not in header]
    if missing:
        raise ImportError_([f"missing column(s): {', '.join(missing)}. Header found: {', '.join(header) or '(empty)'}"])
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for i, raw in enumerate(reader, start=2):
        if i - 1 > MAX_ROWS:
            errors.append(f"more than {MAX_ROWS} rows: split the file")
            break
        r = {k: (v or "").strip() for k, v in raw.items() if k}
        if not any(r.values()):
            continue
        out: dict[str, Any] | None = None
        if kind == "accounts":
            tier = r["plan_tier"].lower()
            if tier not in TIERS:
                errors.append(f"line {i}: plan_tier must be one of {sorted(TIERS)} (got {r['plan_tier']!r})")
            health = (r.get("health") or "green").lower()
            if health not in HEALTH:
                errors.append(f"line {i}: health must be green, yellow or red (got {r.get('health')!r})")
            arr = _num(r["arr_usd"], "arr_usd", i, errors, lo=0, hi=1e9)
            renewal = None
            if r.get("renewal_date"):
                try:
                    renewal = date.fromisoformat(r["renewal_date"])
                except ValueError:
                    errors.append(f"line {i}: renewal_date must be YYYY-MM-DD (got {r['renewal_date']!r})")
            if not r["external_ref"] or not r["name"]:
                errors.append(f"line {i}: external_ref and name are required")
            key = (r["external_ref"],)
            out = {"external_ref": r["external_ref"], "name": r["name"], "plan_tier": tier, "arr_usd": arr,
                   "renewal_date": renewal, "health": health, "csm_email": r.get("csm_email") or None}
        elif kind == "usage":
            fk = resolve_feature(r["feature"], manifest)
            if fk is None:
                errors.append(f"line {i}: feature {r['feature']!r} is not a feature key or event in features.yaml")
            days = _num(r["active_days"], "active_days", i, errors, integer=True, lo=0, hi=31)
            users = _num(r["active_users"], "active_users", i, errors, integer=True, lo=0, hi=10_000_000)
            events = _num(r.get("events") or "0", "events", i, errors, integer=True, lo=0, hi=1e15)
            acct_days = _num(r["account_active_days"], "account_active_days", i, errors, integer=True, lo=0, hi=31) \
                if r.get("account_active_days") else None
            key = (r["account_ref"], fk)
            out = {"account_ref": r["account_ref"], "feature_key": fk, "active_days": days, "active_users": users,
                   "events": events, "account_active_days": acct_days}
        elif kind == "tickets":
            fk = resolve_feature(r["feature"], manifest, allow_tags=True)
            if r["feature"] and fk is None:
                errors.append(f"line {i}: feature {r['feature']!r} is not a feature key, event or support tag")
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                errors.append(f"line {i}: created_at must be an ISO date/time (got {r['created_at']!r})")
                created = None
            minutes = _num(r["handle_minutes"], "handle_minutes", i, errors, integer=True, lo=0, hi=100_000)
            key = (r["external_id"],)
            out = {"external_id": r["external_id"], "account_ref": r.get("account_ref") or None, "feature_key": fk,
                   "created_at": created, "handle_minutes": minutes}
        else:  # costs
            fk = resolve_feature(r["feature"], manifest) if r["feature"] else None
            if r["feature"] and fk is None:
                errors.append(f"line {i}: feature {r['feature']!r} is not in features.yaml")
            ct = r["cost_type"].lower()
            if ct not in COST_TYPES:
                errors.append(f"line {i}: cost_type must be infra or llm (got {r['cost_type']!r})")
            amount = _num(r["amount_usd"], "amount_usd", i, errors, lo=-1e9, hi=1e9)
            key = (i,)  # several lines per feature are allowed and summed
            out = {"feature_key": fk, "cost_type": ct, "amount_usd": amount}
        if key in seen and kind != "costs":
            errors.append(f"line {i}: duplicate row for {key}")
        seen.add(key)
        rows.append(out)
    if errors:
        raise ImportError_(errors)
    if not rows:
        raise ImportError_(["the file has a header but no data rows"])
    return Parsed(kind, rows)
