"""Mapping precedence (spec §4): human override > manifest (Tier 1) > auto-confirmed Tier 2/3 > triage.

Machine evidence below AUTO_CONFIRM never maps anything: it becomes a `mapping_suggestions` row and the
churn or ticket stays booked as *unmapped* until a person confirms it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.manifest import Manifest, glob_specificity, glob_to_regex

AUTO_CONFIRM = 0.80
RANK = {"human": 3, "manifest": 2, "heuristic": 1, "ai": 1}


@dataclass(frozen=True)
class Candidate:
    feature_key: str
    source: str            # human | manifest | heuristic | ai
    confidence: float = 1.0


@dataclass(frozen=True)
class Decision:
    feature_key: str | None
    source: str | None
    action: str            # 'map' | 'suggest' | 'unmapped'
    confidence: float = 0.0


def resolve(candidates: list[Candidate], threshold: float = AUTO_CONFIRM) -> Decision:
    """Pick the winning mapping for one subject from every candidate the tiers produced."""
    humans = [c for c in candidates if c.source == "human"]
    if humans:
        return Decision(humans[-1].feature_key, "human", "map", 1.0)
    manifest = [c for c in candidates if c.source == "manifest"]
    if manifest:
        return Decision(manifest[0].feature_key, "manifest", "map", 1.0)
    machine = sorted((c for c in candidates if c.source in ("heuristic", "ai")), key=lambda c: -c.confidence)
    if machine and machine[0].confidence >= threshold:
        return Decision(machine[0].feature_key, machine[0].source, "map", machine[0].confidence)
    if machine:
        return Decision(machine[0].feature_key, machine[0].source, "suggest", machine[0].confidence)
    return Decision(None, None, "unmapped")


@dataclass
class EffectiveResolver:
    """Path ownership with precedence. Human and machine path mappings are directory globs."""
    manifest: Manifest
    human: list[tuple[str, str]] = field(default_factory=list)                 # (glob, feature key)
    machine: list[tuple[str, str, float]] = field(default_factory=list)        # (glob, feature key, confidence)
    threshold: float = AUTO_CONFIRM

    @staticmethod
    def _best(path: str, globs: list[tuple[str, str]]) -> str | None:
        best = None
        for g, key in globs:
            if glob_to_regex(g).match(path):
                spec = glob_specificity(g)
                if best is None or spec > best[0]:
                    best = (spec, key)
        return best[1] if best else None

    def owner(self, path: str) -> tuple[str | None, str | None]:
        """(feature key, source) or (None, None) when unmapped or ignored."""
        if self.manifest.is_ignored(path):
            return None, None
        h = self._best(path, self.human)
        if h:
            return h, "human"
        m = self.manifest.owner_of(path)
        if m:
            return m, "manifest"
        auto = self._best(path, [(g, k) for g, k, c in self.machine if c >= self.threshold])
        if auto:
            return auto, "heuristic"
        return None, None

    def allocate(self, path_stats: list[dict]) -> dict[str, tuple[float, str]]:
        """Churn-weighted shares like Manifest.allocate_paths, but precedence-aware.
        Returns feature key -> (share, source); shares sum to <= 1 (the rest is unmapped)."""
        per: dict[str, list] = {}
        total = 0.0
        for p in path_stats:
            if self.manifest.is_ignored(p["path"]):
                continue
            churn = int(p.get("additions", 0)) + 0.5 * int(p.get("deletions", 0))
            churn = churn if churn > 0 else 0.5
            total += churn
            key, src = self.owner(p["path"])
            if key:
                slot = per.setdefault(key, [0.0, src])
                slot[0] += churn
                if RANK.get(src, 0) > RANK.get(slot[1], 0):
                    slot[1] = src
        if total <= 0:
            return {}
        return {k: (round(v / total, 5), s) for k, (v, s) in per.items()}
