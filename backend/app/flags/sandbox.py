"""SandboxFlagProvider: no vendor. The desired state is recorded in flag_states by the orchestrator, and
evaluation counts are whatever the seed and the admin-only injection endpoint put in flag_evaluations."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import EvalBucket, FlagState


class SandboxFlagProvider:
    name = "sandbox"
    push_based = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []   # for tests

    async def set_state(self, flag_key: str, state: FlagState, keep_on_for: list[str], comment: str) -> dict[str, Any]:
        self.calls.append((flag_key, state.value, list(keep_on_for)))
        return {"sandbox": True, "state": state.value, "keep_on_for": len(keep_on_for)}

    async def pull_evaluations(self, flag_key: str, since: datetime, until: datetime) -> list[EvalBucket]:
        return []
