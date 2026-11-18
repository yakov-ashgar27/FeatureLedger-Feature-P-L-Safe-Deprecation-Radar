"""Role-based access control: four roles, one permission matrix, enforced by FastAPI dependencies.

The matrix is the single source of truth. The API checks it on every route, `/auth/me` returns the
caller's permissions, and the Next.js UI only renders the controls those permissions allow (the UI
is a convenience; the API is the boundary).
"""
from __future__ import annotations

from enum import Enum

from ..core.sunset import SunsetState


class Role(str, Enum):
    VIEWER = "viewer"
    PM_TECH_LEAD = "pm_tech_lead"
    CS_LEAD = "cs_lead"
    ADMIN = "admin"


# Phase 1 seed roles still present in older databases.
LEGACY_ROLES = {"pm": Role.PM_TECH_LEAD, "eng_lead": Role.PM_TECH_LEAD, "cs": Role.CS_LEAD, "finance": Role.VIEWER}


def normalize_role(value: str | None) -> Role:
    if value in LEGACY_ROLES:
        return LEGACY_ROLES[value]
    try:
        return Role(value)
    except ValueError:
        return Role.VIEWER


V, P, C, A = Role.VIEWER, Role.PM_TECH_LEAD, Role.CS_LEAD, Role.ADMIN

# permission -> roles allowed, with what it covers
PERMISSIONS: dict[str, tuple[frozenset[Role], str]] = {
    "read":                    (frozenset({V, P, C, A}), "P&L ledger, tear-sheets, simulator, sunset board, sync health"),
    "triage:decide":           (frozenset({P, A}), "confirm / reassign / reject mapping suggestions"),
    "graph:run":               (frozenset({P, A}), "run Tier 2 + Tier 3 entity resolution"),
    "pnl:recompute":           (frozenset({P, A}), "recompute the P&L"),
    "sunset:create":           (frozenset({P, A}), "initiate a sunset plan"),
    "sunset:blast_radius":     (frozenset({P, A}), "compute + acknowledge the technical blast radius"),
    "sunset:dossier":          (frozenset({P, C, A}), "generate the customer impact dossier"),
    "sunset:cs_signoff":       (frozenset({C, A}), "sign off the impact dossier, mark accounts contacted"),
    "sunset:approve_canary":   (frozenset({A}), "approve canary_dark_launch (flag off) and its promotion"),
    "sunset:approve_cleanup":  (frozenset({A}), "approve dead_code_pr_opened (opens the cleanup PR) / archive"),
    "sunset:rollback":         (frozenset({P, C, A}), "roll a canary back: flag fully on again"),
    "sunset:cancel":           (frozenset({P, A}), "cancel a sunset plan"),
    "integrations:manage":     (frozenset({A}), "trigger syncs, retry dead letters, GitHub sync"),
    "manifest:sync":           (frozenset({A}), "apply features.yaml"),
    "imports:write":           (frozenset({A}), "CSV imports"),
    "settings:write":          (frozenset({A}), "org_settings: rates, thresholds"),
    "users:manage":            (frozenset({A}), "list users, change roles, disable"),
    "flags:simulate":          (frozenset({A}), "SANDBOX_MODE: inject flag evaluations, simulate a PR merge"),
}

# Which permission a forward transition needs, by the state the plan is leaving.
ADVANCE_PERMISSION: dict[SunsetState, str] = {
    SunsetState.CANDIDATE: "sunset:blast_radius",            # -> blast_radius_checked
    SunsetState.BLAST_RADIUS_CHECKED: "sunset:cs_signoff",   # -> customer_outreach
    SunsetState.CUSTOMER_OUTREACH: "sunset:approve_canary",  # -> canary_dark_launch (zero_usage_off)
    SunsetState.CANARY_DARK_LAUNCH: "sunset:approve_cleanup",  # promote is checked separately
    SunsetState.DEAD_CODE_PR_OPENED: "sunset:approve_cleanup",  # manual archive (normally the merge webhook)
}
CANARY_PROMOTE_PERMISSION = "sunset:approve_canary"

EVIDENCE_PERMISSION = {"blast_radius": "sunset:blast_radius", "cleanup_pr": "sunset:blast_radius",
                       "impact_dossier": "sunset:dossier"}
ACK_PERMISSION = {"inbound": "sunset:blast_radius", "account": "sunset:cs_signoff"}


def can(role: Role | str, permission: str) -> bool:
    if permission not in PERMISSIONS:
        raise KeyError(f"unknown permission {permission!r}")
    return normalize_role(role) in PERMISSIONS[permission][0]


def permissions_for(role: Role | str) -> list[str]:
    r = normalize_role(role)
    return sorted(p for p, (roles, _) in PERMISSIONS.items() if r in roles)


def matrix() -> list[dict]:
    return [{"permission": p, "description": d, **{r.value: r in roles for r in Role}}
            for p, (roles, d) in PERMISSIONS.items()]
