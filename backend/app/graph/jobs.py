"""Graph job: reallocate -> Tier 2 structural inference -> reallocate -> Tier 3 semantic classification.

Runs after every sync (with that job's TextVault), after a features.yaml push, after human triage, and
hourly. Idempotent: it rewrites only what changed and never touches human decisions.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .manifest_store import active_manifest
from .precedence import AUTO_CONFIRM
from .reallocate import load_resolver, reallocate
from .semantic_classifier import TextVault, run_semantic
from .structural_inference import infer


async def run_structural(conn, org_id: str, resolver) -> dict[str, Any]:
    rows = await conn.fetch("""SELECT path_stats FROM work_items
                                WHERE item_type = 'pr' AND NOT is_sweeping AND jsonb_array_length(path_stats) > 0""")
    pr_paths = [[p["path"] for p in r["path_stats"]] for r in rows]
    files = {p for paths in pr_paths for p in paths}
    edges = [(r["from_path"], r["to_path"]) for r in await conn.fetch("SELECT from_path, to_path FROM import_edges")]
    proposals = infer(resolver, files, edges, pr_paths)
    fids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
    stats = {"clusters": len(proposals), "auto_confirmed": 0, "suggested": 0, "demoted": 0}
    for p in proposals:
        fid = fids.get(p.feature_key)
        if fid is None:
            continue
        aid = await conn.fetchval("""
            INSERT INTO code_assets (org_id, asset_type, identifier) VALUES ($1, 'path_glob', $2)
            ON CONFLICT (org_id, repo_id, asset_type, identifier) DO UPDATE SET last_seen = now() RETURNING id""",
            org_id, p.glob)
        decided = await conn.fetchrow("""SELECT status::text AS status FROM mapping_suggestions
                                          WHERE subject_type = 'code_asset' AND subject_id = $1 AND decided_at IS NOT NULL""",
                                      aid)
        if decided:   # a person already confirmed, reassigned or rejected this directory: never second-guess
            continue
        human = await conn.fetchval("""SELECT 1 FROM asset_feature_mappings WHERE asset_id = $1 AND source = 'human'""", aid)
        auto = p.confidence >= AUTO_CONFIRM and not human
        if auto:
            await conn.execute("""DELETE FROM asset_feature_mappings WHERE asset_id = $1 AND source = 'heuristic'
                                  AND feature_id <> $2""", aid, fid)
            await conn.execute("""
                INSERT INTO asset_feature_mappings (asset_id, feature_id, org_id, source, confidence, status)
                VALUES ($1, $2, $3, 'heuristic', $4, 'confirmed')
                ON CONFLICT (asset_id, feature_id) DO UPDATE SET confidence = EXCLUDED.confidence, status = 'confirmed'
                 WHERE asset_feature_mappings.source = 'heuristic'""", aid, fid, org_id, p.confidence)
            stats["auto_confirmed"] += 1
        else:
            gone = await conn.fetchval("""SELECT count(*) FROM asset_feature_mappings
                                           WHERE asset_id = $1 AND source = 'heuristic'""", aid)
            if gone:   # evidence weakened below 0.80: the automatic mapping is withdrawn, back to triage
                await conn.execute("DELETE FROM asset_feature_mappings WHERE asset_id = $1 AND source = 'heuristic'", aid)
            stats["demoted"] += int(gone or 0)
            stats["suggested"] += 1
        await conn.execute("""
            INSERT INTO mapping_suggestions (org_id, subject_type, subject_id, subject_label, suggested_feature_id,
                                             confidence, source, rationale, status, tier, evidence, auto_confirmed,
                                             decided_feature_id)
            VALUES ($1, 'code_asset', $2, $3, $4, $5, 'heuristic', $6, $7::mapping_status, 2, $8, $9, $10)
            ON CONFLICT (org_id, subject_type, subject_id) DO UPDATE SET
                suggested_feature_id = EXCLUDED.suggested_feature_id, confidence = EXCLUDED.confidence,
                source = 'heuristic', rationale = EXCLUDED.rationale, status = EXCLUDED.status, tier = 2,
                evidence = EXCLUDED.evidence, auto_confirmed = EXCLUDED.auto_confirmed,
                decided_feature_id = EXCLUDED.decided_feature_id, updated_at = now()
             WHERE mapping_suggestions.decided_at IS NULL""",
            org_id, aid, f"{p.glob} ({p.files} file{'s' if p.files != 1 else ''})", fid, p.confidence, p.rationale,
            "confirmed" if auto else "pending", {**p.evidence, "threshold": AUTO_CONFIRM}, auto, fid if auto else None)
    return stats


log = logging.getLogger("featureledger.graph")


async def run_graph(conn, org_id: str, vault: TextVault | None = None, embedder=None, explainer=None) -> dict[str, Any]:
    t, timings = time.monotonic(), {}

    def lap(name: str) -> None:
        nonlocal t
        now = time.monotonic()
        timings[name] = round(now - t, 2)
        t = now
        log.info("graph %s: %s done in %.2fs", org_id, name, timings[name])
    manifest = await active_manifest(conn)
    resolver = await load_resolver(conn, manifest)
    before = await reallocate(conn, org_id, resolver)
    lap("reallocate")
    tier2 = await run_structural(conn, org_id, resolver)
    lap("tier2")
    resolver = await load_resolver(conn, manifest)
    after = await reallocate(conn, org_id, resolver)
    lap("reallocate_after_tier2")
    tier3 = await run_semantic(conn, org_id, manifest, vault, embedder, explainer)
    lap("tier3")
    return {"reallocated": before["reallocated"] + after["reallocated"], "protected_human": after["protected_human"],
            "tier2": tier2, "tier3": tier3, "timings_s": timings}
