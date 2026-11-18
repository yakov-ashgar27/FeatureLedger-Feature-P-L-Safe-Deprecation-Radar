"""Tier 1 entity resolution: the `features.yaml` manifest.

Path matching rules
  * `**` matches any number of path segments (including zero), `*` matches within one segment,
    `?` matches one non-separator character.
  * When several features claim a file, the MOST SPECIFIC glob wins: the one with the longest
    literal (wildcard-free) prefix, then the most literal characters. The same glob declared by
    two features is a validation error; on any remaining tie the feature declared first wins.
  * `ignore` globs (lockfiles, generated code) are excluded from churn entirely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
VALID_TIERS = {None, "starter", "pro", "enterprise"}
VALID_RULES = {"none", "proportional_usage", "proportional_cost", "fixed_weights"}
DEFAULT_IGNORE = ["**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "**/poetry.lock",
                  "**/*.min.js", "**/vendor/**", "**/__snapshots__/**", "**/*.generated.*"]


class ManifestError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@lru_cache(maxsize=4096)
def glob_to_regex(glob: str) -> re.Pattern[str]:
    i, out = 0, []
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_specificity(glob: str) -> tuple[int, int]:
    m = re.search(r"[*?]", glob)
    prefix = glob if m is None else glob[: m.start()]
    literal = len(re.sub(r"[*?]", "", glob))
    return len(prefix), literal


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    name: str
    owner_team: str | None = None
    description: str | None = None
    paths: tuple[str, ...] = ()
    api_routes: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    event_aliases: tuple[str, ...] = ()
    flag_key: str | None = None
    jira_labels: tuple[str, ...] = ()
    jira_epics: tuple[str, ...] = ()
    support_tags: tuple[str, ...] = ()
    cost_tags: tuple[str, ...] = ()
    tier_gate: str | None = None
    shared_overhead: bool = False
    allocation_rule: str = "none"
    allocation_weights: dict[str, float] | None = None
    regulatory: bool = False
    launched_at: str | None = None
    replaced_by: str | None = None   # feature key customers should migrate to


@dataclass(frozen=True)
class TeamSpec:
    key: str
    name: str
    headcount: int  # engineers only; drives the capacity calibration (docs/SPEC.md §3)


@dataclass
class Manifest:
    version: int
    features: list[FeatureSpec]
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    teams: list[TeamSpec] = field(default_factory=list)

    def by_key(self) -> dict[str, FeatureSpec]:
        return {f.key: f for f in self.features}

    def is_ignored(self, path: str) -> bool:
        return any(glob_to_regex(g).match(path) for g in self.ignore)

    def owner_of(self, path: str) -> str | None:
        """Feature key owning `path`, or None if unmapped / ignored."""
        if self.is_ignored(path):
            return None
        best: tuple[tuple[int, int], str] | None = None
        for f in self.features:
            for g in f.paths:
                if glob_to_regex(g).match(path):
                    spec = glob_specificity(g)
                    if best is None or spec > best[0]:
                        best = (spec, f.key)
        return best[1] if best else None

    def feature_for_event(self, event: str) -> str | None:
        for f in self.features:
            if event in f.events or event in f.event_aliases:
                return f.key
        return None

    def feature_for_route(self, route: str) -> str | None:
        best: tuple[int, str] | None = None
        for f in self.features:
            for r in f.api_routes:
                prefix = r.rstrip("*")
                if route == r or (r.endswith("*") and route.startswith(prefix)):
                    if best is None or len(prefix) > best[0]:
                        best = (len(prefix), f.key)
        return best[1] if best else None

    def allocate_paths(self, path_stats: list[dict[str, Any]]) -> tuple[dict[str, float], int]:
        """Split a PR's churn across features. Returns (feature_key -> share, counted churn).

        Shares are churn-weighted (additions + 0.5*deletions) over non-ignored files; churn on
        unmapped files leaves the shares summing to < 1 (the remainder is booked as unmapped).
        """
        per: dict[str, float] = {}
        total = 0.0
        for p in path_stats:
            path = p["path"]
            if self.is_ignored(path):
                continue
            churn = int(p.get("additions", 0)) + 0.5 * int(p.get("deletions", 0))
            if churn <= 0:
                churn = 0.5  # renames / mode changes still cost a little attention
            total += churn
            key = self.owner_of(path)
            if key:
                per[key] = per.get(key, 0.0) + churn
        if total <= 0:
            return {}, 0
        return {k: round(v / total, 5) for k, v in per.items()}, int(total)


def _tuple(v: Any, field_name: str, key: str, errors: list[str]) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        errors.append(f"{key}: '{field_name}' must be a list of non-empty strings")
        return ()
    return tuple(v)


def parse_manifest(text: str) -> Manifest:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError([f"invalid YAML: {e}"]) from e
    return manifest_from_dict(doc)


def manifest_from_dict(doc: Any) -> Manifest:
    errors: list[str] = []
    if not isinstance(doc, dict):
        raise ManifestError(["manifest root must be a mapping"])
    version = doc.get("version")
    if version != 1:
        errors.append(f"unsupported manifest version {version!r} (expected 1)")
    raw_features = doc.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise ManifestError(errors + ["'features' must be a non-empty list"])

    specs: list[FeatureSpec] = []
    seen: set[str] = set()
    for i, rf in enumerate(raw_features):
        if not isinstance(rf, dict):
            errors.append(f"features[{i}] must be a mapping")
            continue
        key = rf.get("key")
        label = f"features[{i}]({key})"
        if not isinstance(key, str) or not KEY_RE.match(key):
            errors.append(f"{label}: 'key' must match {KEY_RE.pattern}")
            continue
        if key in seen:
            errors.append(f"{label}: duplicate key")
        seen.add(key)
        if not rf.get("name"):
            errors.append(f"{label}: 'name' is required")
        jira = rf.get("jira") or {}
        tier = rf.get("tier_gate")
        if tier not in VALID_TIERS:
            errors.append(f"{label}: tier_gate must be one of starter|pro|enterprise or null")
        rule = rf.get("allocation_rule", "none")
        shared = bool(rf.get("shared_overhead", False))
        if rule not in VALID_RULES:
            errors.append(f"{label}: allocation_rule must be one of {sorted(VALID_RULES)}")
        if rule != "none" and not shared:
            errors.append(f"{label}: allocation_rule requires shared_overhead: true")
        weights = rf.get("allocation_weights")
        if (rule == "fixed_weights") != (weights is not None):
            errors.append(f"{label}: allocation_weights is required iff allocation_rule is fixed_weights")
        if weights is not None and (not isinstance(weights, dict)
                                    or not all(isinstance(w, (int, float)) and w >= 0 for w in weights.values())):
            errors.append(f"{label}: allocation_weights must map feature keys to non-negative numbers")
        paths = _tuple(rf.get("paths"), "paths", label, errors)
        routes = _tuple(rf.get("api_routes"), "api_routes", label, errors)
        for r in routes:
            if not r.startswith("/"):
                errors.append(f"{label}: api route {r!r} must start with '/'")
        specs.append(FeatureSpec(
            key=key, name=str(rf.get("name", key)), owner_team=rf.get("owner_team"),
            description=rf.get("description"), paths=paths, api_routes=routes,
            events=_tuple(rf.get("events"), "events", label, errors),
            event_aliases=_tuple(rf.get("event_aliases"), "event_aliases", label, errors),
            flag_key=rf.get("flag_key"),
            jira_labels=_tuple(jira.get("labels"), "jira.labels", label, errors),
            jira_epics=_tuple(jira.get("epics"), "jira.epics", label, errors),
            support_tags=_tuple(rf.get("support_tags"), "support_tags", label, errors),
            cost_tags=_tuple(rf.get("cost_tags"), "cost_tags", label, errors),
            tier_gate=tier, shared_overhead=shared, allocation_rule=rule,
            allocation_weights=weights, regulatory=bool(rf.get("regulatory", False)),
            launched_at=str(rf["launched_at"]) if rf.get("launched_at") else None,
            replaced_by=rf.get("replaced_by"),
        ))

    keys = {s.key for s in specs}
    for s in specs:
        if s.replaced_by and s.replaced_by not in keys:
            errors.append(f"{s.key}: replaced_by references unknown feature {s.replaced_by!r}")
        for k in (s.allocation_weights or {}):
            if k not in keys:
                errors.append(f"{s.key}: allocation_weights references unknown feature {k!r}")

    # identical globs or equal-specificity duplicates across features = ambiguous ownership
    owners: dict[str, str] = {}
    for s in specs:
        for g in s.paths:
            if g in owners and owners[g] != s.key:
                errors.append(f"path glob {g!r} claimed by both {owners[g]!r} and {s.key!r}")
            owners[g] = s.key
    events: dict[str, str] = {}
    for s in specs:
        for e in s.events + s.event_aliases:
            if e in events and events[e] != s.key:
                errors.append(f"event {e!r} claimed by both {events[e]!r} and {s.key!r}")
            events[e] = s.key
    flags: dict[str, str] = {}
    for s in specs:
        if s.flag_key:
            if s.flag_key in flags:
                errors.append(f"flag {s.flag_key!r} claimed by both {flags[s.flag_key]!r} and {s.key!r}")
            flags[s.flag_key] = s.key

    teams: list[TeamSpec] = []
    raw_teams = doc.get("teams") or []
    if not isinstance(raw_teams, list):
        errors.append("'teams' must be a list")
        raw_teams = []
    for i, t in enumerate(raw_teams):
        if not isinstance(t, dict) or not isinstance(t.get("key"), str) or not KEY_RE.match(t.get("key", "")):
            errors.append(f"teams[{i}]: 'key' must match {KEY_RE.pattern}")
            continue
        hc = t.get("headcount")
        if not isinstance(hc, int) or hc < 0 or hc > 2000:
            errors.append(f"teams[{i}]({t['key']}): 'headcount' must be an integer between 0 and 2000")
            continue
        teams.append(TeamSpec(t["key"], str(t.get("name") or t["key"]), hc))
    team_keys = {t.key for t in teams}
    if len(team_keys) != len(teams):
        errors.append("duplicate team key in 'teams'")
    if teams:
        for s in specs:
            if s.owner_team and s.owner_team not in team_keys:
                errors.append(f"{s.key}: owner_team {s.owner_team!r} is not declared under 'teams'")

    ignore = doc.get("ignore")
    if ignore is not None and (not isinstance(ignore, list) or not all(isinstance(x, str) for x in ignore)):
        errors.append("'ignore' must be a list of globs")
    if errors:
        raise ManifestError(errors)
    return Manifest(version=1, features=specs, ignore=list(DEFAULT_IGNORE) + list(ignore or []), teams=teams)
