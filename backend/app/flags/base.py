"""The FeatureFlagProvider interface the sunset orchestrator drives.

    fully_on        everyone is served the feature (before the canary; the rollback target)
    zero_usage_off  off for every account with 0 events in the last 90 days, on for `keep_on_for`
    fully_off       off for everyone: the scream test is running

Providers are thin: they translate a state into the vendor's targeting API and report evaluation
counts. Persistence (flag_states, flag_evaluations) and the guards live in the orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class FlagState(str, Enum):
    FULLY_ON = "fully_on"
    ZERO_USAGE_OFF = "zero_usage_off"
    FULLY_OFF = "fully_off"


class FlagProviderError(RuntimeError):
    """The vendor rejected or could not apply the change. The transition is aborted (fail closed)."""


@dataclass(frozen=True)
class EvalBucket:
    bucket_start: datetime
    on_count: int
    off_count: int


class FlagProvider(Protocol):
    name: str
    push_based: bool   # True: evaluation counts arrive by webhook instead of being pulled

    async def set_state(self, flag_key: str, state: FlagState, keep_on_for: list[str], comment: str) -> dict[str, Any]:
        """Apply the targeting for `state`; returns a provider reference (version, ids) for the audit trail."""
        ...

    async def pull_evaluations(self, flag_key: str, since: datetime, until: datetime) -> list[EvalBucket]:
        """Evaluation counts per bucket in [since, until). Push-based providers return []."""
        ...


def check_response(provider: str, action: str, status: int, body: Any) -> None:
    if status >= 400:
        detail = body.get("message") if isinstance(body, dict) else str(body)[:300]
        raise FlagProviderError(f"{provider} {action} failed: HTTP {status} {detail or ''}".strip())
