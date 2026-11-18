"""Stripe (Customers, Subscriptions, Invoices), ARR, plan tier and renewal per account.

Backfill: list endpoints paged with `starting_after` (`subscriptions?status=all`, invoices created in
the backfill window). Incremental: the Events API (`/v1/events?created[gt]=<cursor>`), the pattern Stripe
recommends, because subscription changes never touch `created`. Each event's object is re-scrubbed into
the same envelope types, so processing is identical either way.

ARR rules (pure functions, unit-tested):
  item MRR = unit_amount × quantity / 100 / months_per_interval   (day 1/30.44, week 12/52, month n, year 12n)
  account ARR = 12 × Σ MRR of items on subscriptions in status active | past_due | trialing(paid later)
  history: an invoice line covering k months contributes amount/k to every month it covers
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator

from .base import Batch, Connector, SyncCtx, add_months, content_hash, month_start
from .scrub import scrub_stripe_customer, scrub_stripe_invoice, scrub_stripe_subscription

PAGE = 100
LIVE_STATUSES = {"active", "past_due"}
TIER_RANK = {"free": 0, "starter": 1, "pro": 2, "enterprise": 3}
EVENT_TYPES = ["customer.*", "customer.subscription.*", "invoice.*"]


def months_per_interval(interval: str | None, count: int) -> float:
    return {"day": count / 30.4375, "week": count * 12 / 52, "month": float(count), "year": 12.0 * count}.get(
        interval or "month", float(count))


def to_usd(amount_minor: int, currency: str, fx: dict[str, float]) -> float:
    zero_decimal = {"jpy", "krw", "vnd", "clp", "isk", "huf"}
    major = amount_minor if currency in zero_decimal else amount_minor / 100
    return major * fx.get(currency, 1.0 if currency == "usd" else float("nan"))


def account_state(customer: dict[str, Any], subs: list[dict[str, Any]], fx: dict[str, float]) -> dict[str, Any]:
    """ARR (USD), tier and renewal for one customer from its latest subscription states."""
    mrr, tiers, period_ends = 0.0, [], []
    for s in subs:
        if s.get("status") not in LIVE_STATUSES:
            continue
        for it in s["items"]:
            if not it.get("interval"):
                continue
            mrr += to_usd(it["unit_amount"] * it["quantity"], it["currency"], fx) / \
                months_per_interval(it["interval"], it["interval_count"])
            if it.get("tier"):
                tiers.append(it["tier"])
            if it.get("current_period_end"):
                period_ends.append(it["current_period_end"])
        if s.get("tier"):
            tiers.append(s["tier"])
        if s.get("current_period_end"):
            period_ends.append(s["current_period_end"])
    md = customer.get("metadata") or {}
    tier = max(tiers, key=lambda t: TIER_RANK.get(t, 0)) if tiers else md.get("tier") or ("starter" if mrr else "free")
    renewal = md.get("contract_renewal")
    if not renewal and period_ends:
        renewal = datetime.fromtimestamp(min(period_ends), timezone.utc).date().isoformat()
    return {"arr_usd": round(mrr * 12, 2), "plan_tier": tier if tier in TIER_RANK else "starter",
            "renewal_date": renewal, "health": md.get("health") if md.get("health") in ("green", "yellow", "red") else "green",
            "csm_email": md.get("csm_email")}


def monthly_mrr_from_invoices(invoices: list[dict[str, Any]], fx: dict[str, float]) -> dict[date, float]:
    """Paid subscription lines spread evenly over the months they cover."""
    out: dict[date, float] = defaultdict(float)
    for inv in invoices:
        if inv.get("status") != "paid":
            continue
        for ln in inv["lines"]:
            if not ln.get("subscription") or not ln.get("start") or not ln.get("end"):
                continue
            start = datetime.fromtimestamp(ln["start"], timezone.utc)
            end = datetime.fromtimestamp(ln["end"], timezone.utc)
            months = max(1, round((end - start).days / 30.4375))
            amount = to_usd(ln["amount"], inv["currency"], fx)
            m = month_start(start if start.day <= 15 else add_months(month_start(start), 1))
            for i in range(months):
                out[add_months(m, i)] += amount / months
    return {k: round(v, 2) for k, v in out.items()}


class StripeConnector(Connector):
    kind = "stripe"
    streams = ("customers", "subscriptions", "invoices", "events")

    async def _list(self, ctx: SyncCtx, path: str, entity: str, params: dict[str, Any], scrub) -> AsyncIterator[Batch]:
        after: str | None = None
        while True:
            q = dict(params, limit=PAGE)
            if after:
                q["starting_after"] = after
            r = await self.t.request("GET", path, params=q)
            data = r.body.get("data", [])
            envs = [ctx.envelope(entity, o["id"], content_hash(s), s) for o in data for s in [scrub(o)]]
            more = bool(r.body.get("has_more")) and data
            after = data[-1]["id"] if data else None
            yield Batch(path.rsplit("/", 1)[-1], envs, None if more else "backfilled")
            if not more:
                return

    async def backfill(self, ctx: SyncCtx, stream: str) -> AsyncIterator[Batch]:
        since = int(datetime.combine(add_months(month_start(ctx.now()), -ctx.backfill_months),
                                     datetime.min.time(), timezone.utc).timestamp())
        if stream == "customers":
            async for b in self._list(ctx, "/v1/customers", "stripe_customer", {}, scrub_stripe_customer):
                yield b
        elif stream == "subscriptions":
            async for b in self._list(ctx, "/v1/subscriptions", "stripe_subscription", {"status": "all"},
                                      scrub_stripe_subscription):
                yield b
        elif stream == "invoices":
            async for b in self._list(ctx, "/v1/invoices", "stripe_invoice", {"created[gte]": since},
                                      scrub_stripe_invoice):
                yield b
        else:  # events: start listening from now; everything older came from the list endpoints
            yield Batch("events", [], str(int(ctx.now().timestamp())))

    async def incremental(self, ctx: SyncCtx, stream: str, cursor: str) -> AsyncIterator[Batch]:
        if stream != "events":
            return  # list streams are backfill-only; the events stream carries every later change
        after: str | None = None
        newest = int(cursor)
        while True:
            q: dict[str, Any] = {"created[gt]": int(cursor), "limit": PAGE, "types[]": EVENT_TYPES}
            if after:
                q["starting_after"] = after
            r = await self.t.request("GET", "/v1/events", params=q)
            data = r.body.get("data", [])
            envs = []
            for ev in data:
                envs.extend(self.envelopes_for_event(ctx, ev))
                newest = max(newest, int(ev.get("created") or 0))
            more = bool(r.body.get("has_more")) and data
            after = data[-1]["id"] if data else None
            yield Batch("events", envs, None if more else str(newest))
            if not more:
                return

    def envelopes_for_event(self, ctx: SyncCtx, ev: dict[str, Any]) -> list:
        obj = (ev.get("data") or {}).get("object") or {}
        kind = obj.get("object")
        if kind == "customer":
            s = scrub_stripe_customer(obj)
            return [ctx.envelope("stripe_customer", s["id"], content_hash(s), s)]
        if kind == "subscription":
            s = scrub_stripe_subscription(obj)
            return [ctx.envelope("stripe_subscription", s["id"], content_hash(s), s)]
        if kind == "invoice":
            s = scrub_stripe_invoice(obj)
            return [ctx.envelope("stripe_invoice", s["id"], content_hash(s), s)]
        return []

    def handle_webhook(self, ctx: SyncCtx, headers: dict[str, str], body: bytes) -> list:
        return self.envelopes_for_event(ctx, json.loads(body or b"{}"))
