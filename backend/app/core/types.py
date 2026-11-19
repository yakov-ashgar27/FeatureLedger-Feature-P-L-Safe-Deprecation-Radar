"""Plain data types shared by the pure P&L engine.

The engine (app.core.*) is deliberately free of I/O: the database layer and the seed generator
both produce a `Dataset`, which makes every formula unit-testable and replayable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class WorkKind(str, Enum):
    FEATURE = "feature"
    BUG = "bug"
    CHORE = "chore"
    HOTFIX = "hotfix"
    INCIDENT = "incident"
    REVERT = "revert"


class FeatureClass(str, Enum):
    CASH_COW = "cash_cow"
    GROWTH_BET = "growth_bet"
    NICHE_ANCHOR = "niche_anchor"
    ZOMBIE = "zombie"
    WATCH = "watch"
    MANDATED = "mandated"
    PLATFORM = "platform"


class AttributionModel(str, Enum):
    USAGE_WEIGHTED = "usage_weighted"
    TIER_GATED = "tier_gated"
    CRITICAL_DEPENDENCY = "critical_dependency"


@dataclass(frozen=True)
class OrgSettings:
    eng_hourly_rate_usd: float = 95.0
    support_hourly_rate_usd: float = 45.0
    hours_per_engineer_month: float = 140.0
    allocatable_fraction: float = 0.80
    calibration_clamp_max: float = 4.0
    critical_concentration: float = 0.40
    anchor_min_arr_usd: float = 100_000.0
    tier_uplift_share: dict[str, float] = field(default_factory=lambda: {"enterprise": 0.35, "pro": 0.20})
    # classification thresholds (documented in docs/SPEC.md §4.5)
    zombie_max_adoption: float = 0.05
    zombie_min_monthly_cost: float = 2_000.0
    zombie_min_bug_tax: float = 0.40
    anchor_max_adoption: float = 0.10
    growth_max_age_months: int = 9
    growth_min_mom_adoption: float = 0.10
    cash_cow_min_efficiency: float = 2.0
    # Phase 5: AI gross margin ("margin_bleed": popular, but token spend eats the revenue)
    margin_bleed_min_adoption: float = 0.15
    margin_bleed_max_gross_margin: float = 0.20


@dataclass(frozen=True)
class Team:
    id: str
    key: str
    name: str
    headcount: int


@dataclass(frozen=True)
class Feature:
    id: str
    key: str
    name: str
    owner_team_id: str | None = None
    tier_gate: str | None = None
    is_shared_overhead: bool = False
    allocation_rule: str = "none"
    allocation_weights: dict[str, float] | None = None
    is_regulatory: bool = False
    launched_at: date | None = None
    flag_key: str | None = None


@dataclass(frozen=True)
class WorkItem:
    id: str
    team_id: str
    period_month: date
    kind: WorkKind
    priority: str | None
    additions: int
    deletions: int
    files_changed: int
    review_rounds: int
    incident_response_hours: float = 0.0
    is_sweeping: bool = False
    # feature_id -> share of this item's effort (sum <= 1, remainder is unmapped)
    allocations: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    plan_tier: str
    arr_usd: float
    renewal_date: date | None
    health: str  # green | yellow | red


@dataclass(frozen=True)
class Usage:
    account_id: str
    feature_id: str
    period_month: date
    events: int
    active_days: int
    active_users: int


@dataclass(frozen=True)
class Ticket:
    id: str
    feature_id: str | None
    account_id: str | None
    period_month: date
    handle_minutes: int


@dataclass(frozen=True)
class CostLine:
    feature_id: str | None
    period_month: date
    cost_type: str  # infra | llm
    amount_usd: float


@dataclass
class Dataset:
    """Everything the engine needs for one org. Accounts are month-end snapshots keyed by month."""
    settings: OrgSettings
    teams: list[Team]
    features: list[Feature]
    work_items: list[WorkItem]
    accounts_by_month: dict[date, list[Account]]
    usage: list[Usage]
    account_active_days: dict[tuple[date, str], int]  # (month, account_id) -> distinct active days
    tickets: list[Ticket]
    cost_lines: list[CostLine]
