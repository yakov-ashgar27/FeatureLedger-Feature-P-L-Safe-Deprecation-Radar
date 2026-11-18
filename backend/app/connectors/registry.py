"""Build a connector for an `integrations` row: live (credentials from the environment) or sandbox."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .aws_ce import AwsCostExplorerConnector, Boto3CostExplorer
from .base import Connector, NotConfigured, SyncCtx
from .jira import JiraConnector
from .posthog import PostHogConnector
from .ratelimit import LIMITS, BucketStore, wrap
from .stripe_billing import StripeConnector
from .zendesk import ZendeskConnector

SUPPORTED = ("stripe", "jira", "posthog", "zendesk", "aws_cost_explorer")
# dependency order for a full sync: accounts first, so every later source can be stitched to them
SYNC_ORDER = ("stripe", "zendesk", "posthog", "jira", "aws_cost_explorer")

ENV_REQUIRED: dict[str, tuple[str, ...]] = {
    "stripe": ("STRIPE_API_KEY",),
    "jira": ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"),
    "posthog": ("POSTHOG_PROJECT_ID", "POSTHOG_PERSONAL_API_KEY"),
    "zendesk": ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"),
    "aws_cost_explorer": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
}


def live_configured(kind: str, env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return all(env.get(k) for k in ENV_REQUIRED.get(kind, ("__never__",)))


def fx_rates(env: dict[str, str] | None = None) -> dict[str, float]:
    env = os.environ if env is None else env
    rates = {"usd": 1.0}
    rates.update({k.lower(): float(v) for k, v in json.loads(env.get("FX_RATES_TO_USD") or "{}").items()})
    return rates


@dataclass
class Built:
    connector: Connector
    ctx: SyncCtx
    close: Callable[[], Awaitable[None]]


async def _noop() -> None:
    return None


def build(kind: str, org_id: str, integration_id: str, config: dict[str, Any], sandbox: bool,
          store: BucketStore, manifest_text: str, env: dict[str, str] | None = None) -> Built:
    env = dict(os.environ) if env is None else env
    if kind not in SUPPORTED:
        raise NotConfigured(f"no Phase 2 connector for {kind!r}")
    backfill_months = int(env.get("CONNECTOR_BACKFILL_MONTHS", "6"))

    if sandbox:
        from . import sandbox as sb
        ctx = SyncCtx(org_id, integration_id, config, clock=sb.sandbox_clock, backfill_months=backfill_months)
        if kind == "aws_cost_explorer":
            return Built(AwsCostExplorerConnector(sb.SandboxCostExplorer(manifest_text)), ctx, _noop)
        router = {"stripe": sb.stripe_router, "jira": sb.jira_router, "posthog": sb.posthog_router,
                  "zendesk": sb.zendesk_router}[kind](manifest_text)
        t = wrap(kind, integration_id, sb.SandboxTransport(router), store)
        return Built(_make(kind, t, config, env, sandbox=True), ctx, _noop)

    if not live_configured(kind, env):
        missing = [k for k in ENV_REQUIRED[kind] if not env.get(k)]
        raise NotConfigured(f"set {', '.join(missing)} in .env to enable the live {kind} connector")
    ctx = SyncCtx(org_id, integration_id, config, backfill_months=backfill_months)
    if kind == "aws_cost_explorer":
        rate, cap = LIMITS[kind]

        async def limiter() -> None:
            import asyncio
            while (wait := await store.take(f"{integration_id}:ce", rate, cap)) > 0:
                await asyncio.sleep(wait)
        api = Boto3CostExplorer(env.get("AWS_REGION", "us-east-1"), limiter)
        return Built(AwsCostExplorerConnector(api, env.get("AWS_CE_TAG_KEY", config.get("tag_key", "feature"))), ctx, _noop)

    from .http import HttpxTransport
    if kind == "stripe":
        inner = HttpxTransport("https://api.stripe.com", {"Authorization": f"Bearer {env['STRIPE_API_KEY']}",
                                                          "Stripe-Version": env.get("STRIPE_API_VERSION", "2024-06-20")})
    elif kind == "jira":
        tok = base64.b64encode(f"{env['JIRA_EMAIL']}:{env['JIRA_API_TOKEN']}".encode()).decode()
        inner = HttpxTransport(env["JIRA_BASE_URL"].rstrip("/"), {"Authorization": f"Basic {tok}",
                                                                   "Accept": "application/json"})
    elif kind == "posthog":
        inner = HttpxTransport(env.get("POSTHOG_HOST", "https://us.posthog.com").rstrip("/"),
                               {"Authorization": f"Bearer {env['POSTHOG_PERSONAL_API_KEY']}"})
    else:  # zendesk
        tok = base64.b64encode(f"{env['ZENDESK_EMAIL']}/token:{env['ZENDESK_API_TOKEN']}".encode()).decode()
        inner = HttpxTransport(f"https://{env['ZENDESK_SUBDOMAIN']}.zendesk.com", {"Authorization": f"Basic {tok}"})
    return Built(_make(kind, wrap(kind, integration_id, inner, store), config, env, sandbox=False), ctx, inner.close)


def _make(kind: str, t, config: dict[str, Any], env: dict[str, str], sandbox: bool) -> Connector:
    if kind == "stripe":
        return StripeConnector(t)
    if kind == "jira":
        projects = config.get("projects") or [p for p in env.get("JIRA_PROJECTS", "").split(",") if p]
        return JiraConnector(t, projects)
    if kind == "posthog":
        return PostHogConnector(t, str(config.get("project_id") or env.get("POSTHOG_PROJECT_ID", "1")),
                                int(config.get("group_type_index", env.get("POSTHOG_GROUP_TYPE_INDEX", 0))))
    from .sandbox import SANDBOX_TIME_FIELD_ID
    field = SANDBOX_TIME_FIELD_ID if sandbox else (int(env["ZENDESK_TIME_SPENT_FIELD_ID"])
                                                   if env.get("ZENDESK_TIME_SPENT_FIELD_ID") else None)
    return ZendeskConnector(t, field)
