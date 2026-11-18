"""Tier 2: structural inference for code that no manifest glob owns.

Unmapped files are clustered into directories (generic leaf folders such as handlers/, lib/ and tests/ are
folded into their parent). Each cluster is scored against every feature on three independent signals:

  imports    import-graph edges between the cluster and the feature's files (either direction)
               s_imp  = share_of_cluster_edges × (1 − 0.5^edges)          more edges, more certainty
  co-change  PRs that touch the cluster AND the feature, out of all PRs touching the cluster
               s_co   = share × (1 − 0.5^prs)
  naming     specific path tokens of the cluster also present in the feature's key, name and globs
               s_name = |shared| / |cluster tokens|

  confidence = 1 − (1 − 0.55·s_imp)(1 − 0.55·s_co)(1 − 0.45·s_name)
               × (1 − 0.3·(runner_up / best)²)   ambiguity: a close runner-up costs a lot, a distant one little
               capped at 0.97.

Only proposals at or above 0.80 are applied automatically; everything else goes to the triage queue.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .precedence import EffectiveResolver

GENERIC_LEAVES = {"handlers", "handler", "models", "model", "lib", "libs", "utils", "util", "tests", "test",
                  "__tests__", "spec", "components", "hooks", "api", "internal", "helpers", "fixtures"}
GENERIC_TOKENS = GENERIC_LEAVES | {"web", "src", "services", "service", "features", "feature", "app", "apps",
                                   "packages", "pkg", "core", "py", "ts", "tsx", "js", "go", "index", "main",
                                   "the", "and", "of", "v2", "new"}
TOKEN_RE = re.compile(r"[a-z0-9]+")
MAX_CONF = 0.97


def tokens(text: str) -> set[str]:
    return {t for t in TOKEN_RE.findall(text.lower()) if t not in GENERIC_TOKENS and len(t) > 1}


def cluster_root(path: str) -> str:
    parts = path.split("/")[:-1]
    while len(parts) > 1 and parts[-1].lower() in GENERIC_LEAVES:
        parts.pop()
    return "/".join(parts) or "."


@dataclass
class Proposal:
    directory: str
    glob: str
    feature_key: str
    confidence: float
    rationale: str
    evidence: dict = field(default_factory=dict)
    files: int = 0


def _feature_tokens(resolver: EffectiveResolver) -> dict[str, set[str]]:
    out = {}
    for f in resolver.manifest.features:
        if f.shared_overhead:
            continue
        lit = " ".join(g.split("*")[0] for g in f.paths)
        out[f.key] = tokens(f"{f.key} {f.name} {lit}")
    return out


def infer(resolver: EffectiveResolver, files: set[str], edges: list[tuple[str, str]],
          pr_paths: list[list[str]], min_files: int = 1) -> list[Proposal]:
    owner = {p: resolver.owner(p)[0] for p in files | {e for edge in edges for e in edge}}
    unmapped = {p for p, k in owner.items() if k is None and not resolver.manifest.is_ignored(p)}
    clusters: dict[str, set[str]] = defaultdict(set)
    for p in unmapped:
        clusters[cluster_root(p)].add(p)
    ftoks = _feature_tokens(resolver)
    product = set(ftoks)
    out: list[Proposal] = []
    for root, members in sorted(clusters.items()):
        if len(members) < min_files or root == ".":
            continue
        # imports
        imp: dict[str, int] = defaultdict(int)
        mods: dict[str, set[str]] = defaultdict(set)
        for a, b in edges:
            for mine, other in ((a, b), (b, a)):
                if mine in members and owner.get(other) in product:
                    imp[owner[other]] += 1
                    mods[owner[other]].add(other)
        imp_total = sum(imp.values())
        # co-change
        n_r, co = 0, defaultdict(int)
        for paths in pr_paths:
            if not any(p in members for p in paths):
                continue
            n_r += 1
            for k in {owner.get(p) or resolver.owner(p)[0] for p in paths if p not in members}:
                if k in product:
                    co[k] += 1
        rtoks = tokens(root)
        scores: list[tuple[float, str, dict]] = []
        for k in product:
            s_imp = (imp[k] / imp_total) * (1 - 0.5 ** imp[k]) if imp_total else 0.0
            s_co = (co[k] / n_r) * (1 - 0.5 ** co[k]) if n_r else 0.0
            shared = sorted(rtoks & ftoks[k])
            s_name = len(shared) / len(rtoks) if rtoks else 0.0
            if not (s_imp or s_co or s_name):
                continue
            conf = 1 - (1 - 0.55 * s_imp) * (1 - 0.55 * s_co) * (1 - 0.45 * s_name)
            scores.append((conf, k, {"imports": imp[k], "modules": len(mods[k]), "co_changed_prs": co[k],
                                     "cluster_prs": n_r, "shared_tokens": shared,
                                     "s_imports": round(s_imp, 3), "s_cochange": round(s_co, 3),
                                     "s_naming": round(s_name, 3)}))
        if not scores:
            continue
        scores.sort(reverse=True)
        conf, key, ev = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 0.0
        if runner_up > 0:   # a close runner-up costs a lot, a distant one almost nothing
            conf *= 1 - 0.3 * (runner_up / conf) ** 2
            ev["runner_up"] = {"feature": scores[1][1], "confidence": round(runner_up, 3)}
        conf = round(min(conf, MAX_CONF), 2)   # stored as numeric(3,2): decide at that precision
        out.append(Proposal(root, f"{root}/**", key, conf, rationale(key, ev), ev, len(members)))
    return out


def rationale(key: str, ev: dict) -> str:
    parts = []
    if ev["imports"]:
        parts.append(f"Imports {ev['modules']} module{'s' if ev['modules'] != 1 else ''} from {key}"
                     if ev["modules"] else f"{ev['imports']} import edges with {key}")
    if ev["co_changed_prs"]:
        parts.append(f"changed together with {key} in {ev['co_changed_prs']} of {ev['cluster_prs']} PRs")
    if ev["shared_tokens"]:
        parts.append(f"path shares '{', '.join(ev['shared_tokens'])}'")
    if ev.get("runner_up"):
        parts.append(f"runner-up {ev['runner_up']['feature']}")
    s = "; ".join(parts)
    return s[:1].upper() + s[1:] if s else f"Weak structural link to {key}"
