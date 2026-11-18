"""AWS Cost Explorer: monthly UnblendedCost grouped by the `feature` cost-allocation tag and SERVICE.

Cost Explorer is not plain REST (SigV4, JSON-RPC), so this connector talks to a `CostExplorerApi`:
the live one wraps boto3 (run in a thread, behind the same token bucket), the sandbox one serves fixtures.
Months are restated by AWS for a few days after month end, so incremental syncs always re-read the
previous and current month; the processor rebuilds a month from the latest envelopes.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, AsyncIterator, Protocol

from .base import Batch, Connector, SyncCtx, add_months, month_start

LLM_SERVICES = {"Amazon Bedrock", "Amazon SageMaker", "Claude (Amazon Bedrock Edition)"}


class CostExplorerApi(Protocol):
    async def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]: ...


class Boto3CostExplorer:
    def __init__(self, region: str, limiter=None):
        import boto3  # only needed in live mode
        self._ce = boto3.client("ce", region_name=region)
        self._limiter = limiter

    async def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        if self._limiter:
            await self._limiter()
        return await asyncio.to_thread(self._ce.get_cost_and_usage, **kwargs)


def split_tag(key: str, tag: str) -> str | None:
    """CE returns 'feature$legacy-pdf-export' (or 'feature$' when untagged)."""
    prefix = f"{tag}$"
    v = key[len(prefix):] if key.startswith(prefix) else key
    return v or None


def cost_type_for(service: str) -> str:
    return "llm" if service in LLM_SERVICES else "infra"


class AwsCostExplorerConnector(Connector):
    kind = "aws_cost_explorer"
    streams = ("costs",)

    def __init__(self, api: CostExplorerApi, tag_key: str = "feature"):
        super().__init__(transport=None)  # type: ignore[arg-type]
        self.api = api
        self.tag = tag_key

    async def _month(self, ctx: SyncCtx, m: date) -> Batch:
        envs, token = [], None
        while True:
            kw: dict[str, Any] = {
                "TimePeriod": {"Start": m.isoformat(), "End": add_months(m, 1).isoformat()},
                "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "TAG", "Key": self.tag}, {"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            if token:
                kw["NextPageToken"] = token
            r = await self.api.get_cost_and_usage(**kw)
            for period in r.get("ResultsByTime", []):
                for g in period.get("Groups", []):
                    tag_key, service = g["Keys"][0], g["Keys"][1]
                    amt = round(float(g["Metrics"]["UnblendedCost"]["Amount"]), 4)
                    p = {"month": m.isoformat(), "feature_tag": split_tag(tag_key, self.tag), "service": service,
                         "cost_type": cost_type_for(service), "amount_usd": amt,
                         "unit": g["Metrics"]["UnblendedCost"].get("Unit", "USD")}
                    envs.append(ctx.envelope("aws_cost", f"{m.isoformat()}|{p['feature_tag'] or ''}|{service}",
                                             str(amt), p))
            token = r.get("NextPageToken")
            if not token:
                return Batch("costs", envs, m.isoformat())

    async def backfill(self, ctx: SyncCtx, stream: str) -> AsyncIterator[Batch]:
        now = month_start(ctx.now())
        m = add_months(now, -ctx.backfill_months)
        while m <= now:
            yield await self._month(ctx, m)
            m = add_months(m, 1)

    async def incremental(self, ctx: SyncCtx, stream: str, cursor: str) -> AsyncIterator[Batch]:
        now = month_start(ctx.now())
        m = min(add_months(now, -1), date.fromisoformat(cursor[:10]))
        while m <= now:
            yield await self._month(ctx, m)
            m = add_months(m, 1)
