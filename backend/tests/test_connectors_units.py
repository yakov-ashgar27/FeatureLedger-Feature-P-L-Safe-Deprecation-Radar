import asyncio
import base64
import hashlib
import hmac
import random
from datetime import date, datetime, timezone

import pytest

from app.connectors.aws_ce import cost_type_for, split_tag
from app.connectors.base import HttpError, HttpResponse, SyncCtx, idempotency_key
from app.connectors.jira import kind_of, parse_jira_ts, priority_of
from app.connectors.normalize import feature_for_tags, usage_month
from app.connectors.ratelimit import MemoryBucketStore, RetryingTransport, backoff_seconds
from app.connectors.scrub import email_domain, scrub_stripe_customer, scrub_zendesk_ticket
from app.connectors.stitching import AccountIndex, resolve
from app.connectors.stripe_billing import account_state, monthly_mrr_from_invoices, months_per_interval
from app.connectors.webhooks import BadSignature, verify_hub_signature, verify_stripe, verify_zendesk
from app.connectors.zendesk import handle_minutes
from app.core.manifest import parse_manifest

M = parse_manifest("""
version: 1
features:
  - {key: legacy-pdf-export, name: Legacy PDF, events: [pdf_exported], event_aliases: [export_pdf_clicked], support_tags: [pdf-export]}
  - {key: editor, name: Editor, events: [doc_edited]}
  - {key: auth, name: Auth, shared_overhead: true}
""")


def ts(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


# ------------------------------------------------------------------ idempotency
def test_idempotency_key_depends_on_version_only():
    a = idempotency_key("i", "stripe_invoice", "in_1", "v1")
    assert a == idempotency_key("i", "stripe_invoice", "in_1", "v1")
    assert a != idempotency_key("i", "stripe_invoice", "in_1", "v2")
    assert a != idempotency_key("j", "stripe_invoice", "in_1", "v1")


# ------------------------------------------------------------------ rate limiting & retries
def test_token_bucket_refills_over_time():
    now = [0.0]
    store = MemoryBucketStore(clock=lambda: now[0])

    async def run():
        waits = [await store.take("k", rate=2.0, capacity=3) for _ in range(4)]
        now[0] += 1.0  # 2 tokens refill
        after = [await store.take("k", 2.0, 3) for _ in range(2)]
        return waits, after
    waits, after = asyncio.run(run())
    assert waits[:3] == [0, 0, 0] and waits[3] == pytest.approx(0.5)
    assert after == [0, 0]


def test_backoff_prefers_server_hints_and_is_bounded():
    assert backoff_seconds(0, retry_after="7") == 7
    assert backoff_seconds(0, reset_epoch="1000", now=990) == 11
    rng = random.Random(3)
    assert all(0 <= backoff_seconds(a, rng=rng) <= min(600, 2 ** a) for a in range(15))


class Scripted:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.n = 0

    async def request(self, method, url, **kw):
        self.n += 1
        s = self.statuses.pop(0)
        return HttpResponse(s, {"Retry-After": "2"} if s == 429 else {}, {"ok": s < 400})


def test_retrying_transport_retries_429_and_5xx_then_succeeds():
    slept = []

    async def sleep(s):
        slept.append(s)
    inner = Scripted([429, 503, 200])
    t = RetryingTransport(inner, MemoryBucketStore(), "k", 100, 100, sleep=sleep, rng=random.Random(1))
    r = asyncio.run(t.request("GET", "https://api.stripe.com/v1/customers"))
    assert r.status == 200 and inner.n == 3 and slept[0] == 2.0 and t.stats["retries"] == 2


def test_retrying_transport_gives_up_and_never_retries_4xx():
    async def sleep(s):
        return None
    t = RetryingTransport(Scripted([500] * 6), MemoryBucketStore(), "k", 100, 100, max_attempts=6, sleep=sleep)
    with pytest.raises(HttpError):
        asyncio.run(t.request("GET", "https://x/y"))
    bad = Scripted([401, 200])
    t2 = RetryingTransport(bad, MemoryBucketStore(), "k", 100, 100, sleep=sleep)
    with pytest.raises(HttpError) as e:
        asyncio.run(t2.request("GET", "https://x/y"))
    assert e.value.status == 401 and bad.n == 1


def test_transport_waits_for_tokens():
    now = [0.0]
    store = MemoryBucketStore(clock=lambda: now[0])
    slept = []

    async def sleep(s):
        slept.append(s)
        now[0] += s
    t = RetryingTransport(Scripted([200] * 3), store, "k", rate=1.0, capacity=1, sleep=sleep)

    async def go():
        for _ in range(3):
            await t.request("GET", "https://h/x")
    asyncio.run(go())
    assert slept == [1.0, 1.0] and t.stats["throttled_waits"] == 2


# ------------------------------------------------------------------ scrubbers
def test_stripe_customer_scrub_keeps_domain_not_person():
    s = scrub_stripe_customer({"id": "cus_1", "email": "Jane.Doe@Globex.com", "name": "Jane Doe", "phone": "1",
                               "metadata": {"company_name": "Globex", "tier": "enterprise", "secret": "x"}})
    assert s["name"] == "Globex" and s["email_domain"] == "globex.com"
    assert "Jane" not in str(s) and "secret" not in str(s) and "phone" not in s
    assert email_domain("someone@gmail.com") is None


def test_zendesk_ticket_scrub_and_handle_time():
    raw = {"id": 5, "created_at": "2026-10-01T10:00:00Z", "updated_at": "2026-10-02T10:00:00Z", "organization_id": 9,
           "tags": ["pdf-export"], "subject": "x" * 500, "description": "full thread", "requester_id": 1,
           "assignee_id": 2, "custom_fields": [{"id": 900, "value": "2700"}]}
    s = scrub_zendesk_ticket(raw, {"replies": 3}, 900)
    assert "description" not in s and "requester_id" not in s and len(s["subject"]) == 120
    assert handle_minutes(s) == (45, False)
    assert handle_minutes(scrub_zendesk_ticket(raw, {"replies": 3}, None)) == (36, True)


# ------------------------------------------------------------------ Stripe ARR math
def test_arr_monthly_annual_currency_and_inactive():
    subs = [
        {"status": "active", "items": [{"unit_amount": 50000, "quantity": 2, "currency": "usd", "interval": "month",
                                        "interval_count": 1, "tier": "pro", "current_period_end": ts(2026, 12)}]},
        {"status": "active", "items": [{"unit_amount": 1200000, "quantity": 1, "currency": "eur", "interval": "year",
                                        "interval_count": 1, "tier": "enterprise", "current_period_end": ts(2027, 3)}]},
        {"status": "canceled", "items": [{"unit_amount": 999999, "quantity": 1, "currency": "usd", "interval": "month",
                                          "interval_count": 1, "tier": "pro"}]},
    ]
    st = account_state({"metadata": {"health": "yellow"}}, subs, {"usd": 1.0, "eur": 1.10})
    assert st["arr_usd"] == pytest.approx(1000 * 12 + 12000 * 1.10)
    assert st["plan_tier"] == "enterprise" and st["health"] == "yellow" and st["renewal_date"] == "2026-12-01"
    assert months_per_interval("week", 1) == pytest.approx(12 / 52)
    free = account_state({"metadata": {"tier": "free"}}, [], {"usd": 1.0})
    assert free["arr_usd"] == 0 and free["plan_tier"] == "free"


def test_invoice_history_spreads_annual_lines():
    inv = [{"status": "paid", "currency": "usd", "lines": [
        {"amount": 1200000, "start": ts(2026, 1), "end": ts(2027, 1), "subscription": "sub_1"}]},
        {"status": "paid", "currency": "usd", "lines": [
            {"amount": 50000, "start": ts(2026, 5), "end": ts(2026, 6), "subscription": "sub_2"}]},
        {"status": "open", "currency": "usd", "lines": [
            {"amount": 777, "start": ts(2026, 5), "end": ts(2026, 6), "subscription": "sub_3"}]}]
    mrr = monthly_mrr_from_invoices(inv, {"usd": 1.0})
    assert mrr[date(2026, 3, 1)] == pytest.approx(1000)
    assert mrr[date(2026, 5, 1)] == pytest.approx(1500)
    assert date(2027, 1, 1) not in mrr


# ------------------------------------------------------------------ identity stitching
def test_stitching_order_and_safety():
    idx = AccountIndex()
    idx.add_account("A", "cus_A", "globex.com")
    idx.add_account("B", "cus_B", "initech.com")
    idx.add_account("C", "cus_C", "initech.com")        # two accounts share a domain
    idx.add_account("D", "cus_D", "gmail.com")          # free mail never indexed
    idx.add_identity("zendesk", "77", "B")
    assert resolve(idx, "stripe", "cus_A").method == "stripe_ref"
    assert resolve(idx, "zendesk", "77", ["globex.com"]).account_id == "B"   # identity beats domain
    assert resolve(idx, "posthog", "g1", ["www.Globex.com"]).account_id == "A"
    assert resolve(idx, "posthog", "g2", ["initech.com"]).method == "ambiguous_domain"
    assert resolve(idx, "posthog", "g3", ["gmail.com"]).method == "unresolved"


def test_usage_month_aggregates_aliases_and_routes_unresolved():
    rows = [{"group_key": "g1", "event": "pdf_exported", "active_days": 3, "active_users": 2, "events": 10},
            {"group_key": "g1", "event": "export_pdf_clicked", "active_days": 5, "active_users": 1, "events": 4},
            {"group_key": "gX", "event": "doc_edited", "active_days": 9, "active_users": 3, "events": 50},
            {"group_key": "g1", "event": "$pageview", "active_days": 20, "active_users": 9, "events": 900}]
    um = usage_month(date(2026, 10, 1), rows, [{"group_key": "g1", "active_days": 12}], M, {"g1": "A"}, "UNRES")
    assert um.usage[("A", "legacy-pdf-export")] == {"events": 14, "active_days": 5, "active_users": 2}
    assert um.usage[("UNRES", "editor")]["events"] == 50
    assert um.unknown_events == {"$pageview"} and um.unresolved_groups == {"gX"}
    assert um.activity["A"] == 12


def test_feature_for_tags():
    assert feature_for_tags(["vip", "pdf-export"], M) == "legacy-pdf-export"
    assert feature_for_tags(["editor"], M) == "editor"
    assert feature_for_tags(["auth"], M) is None       # shared overhead never takes tickets by key
    assert feature_for_tags(["question"], M) is None


# ------------------------------------------------------------------ Jira / AWS
def test_jira_mapping():
    assert kind_of({"issuetype": "Bug"}) == "bug" and kind_of({"issuetype": "Story"}) == "feature"
    assert priority_of({"priority": "Highest"}) == "P0" and priority_of({"priority": None}) is None
    assert parse_jira_ts("2026-10-03T09:12:44.000+0000") == datetime(2026, 10, 3, 9, 12, 44, tzinfo=timezone.utc)


def test_aws_tags_and_services():
    assert split_tag("feature$legacy-pdf-export", "feature") == "legacy-pdf-export"
    assert split_tag("feature$", "feature") is None
    assert cost_type_for("Amazon Bedrock") == "llm" and cost_type_for("Amazon Simple Storage Service") == "infra"


# ------------------------------------------------------------------ webhooks
def test_webhook_signatures():
    body = b'{"type":"invoice.paid"}'
    t = 1_800_000_000
    sig = hmac.new(b"whsec", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    verify_stripe("whsec", f"t={t},v1={sig}", body, now=t + 10)
    with pytest.raises(BadSignature):
        verify_stripe("whsec", f"t={t},v1={sig}", body, now=t + 1000)
    with pytest.raises(BadSignature):
        verify_stripe("whsec", f"t={t},v1={'0' * 64}", body, now=t)
    zsig = base64.b64encode(hmac.new(b"zs", b"2026" + body, hashlib.sha256).digest()).decode()
    verify_zendesk("zs", zsig, "2026", body)
    with pytest.raises(BadSignature):
        verify_zendesk("zs", zsig, "2027", body)
    verify_hub_signature("js", "sha256=" + hmac.new(b"js", body, hashlib.sha256).hexdigest(), body)
    with pytest.raises(BadSignature):
        verify_hub_signature("js", "sha256=bad", body)


def test_sync_ctx_envelope_carries_identity():
    ctx = SyncCtx("o", "i", clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    e = ctx.envelope("jira_issue", "NW-1", "v", {"key": "NW-1"})
    assert e.idempotency_key == idempotency_key("i", "jira_issue", "NW-1", "v")


def test_jira_row_binds_datetimes_not_strings():
    from app.connectors.normalize import jira_row
    r = jira_row({"key": "NW-1", "issuetype": "Bug", "priority": "High", "created": "2026-10-03T09:12:44.000+0000",
                  "resolved": None, "labels": []}, M)
    assert isinstance(r["created"], datetime) and r["resolved"] is None and r["kind"] == "bug"
