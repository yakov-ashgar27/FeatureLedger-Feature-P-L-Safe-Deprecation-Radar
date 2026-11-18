"""Re-allocate PR effort whenever the Feature Graph changes (manifest push, Tier 2/3, human triage).

Precedence per PR:
  * a PR a person decided in triage (directly, or through its linked Jira issue) is never touched;
  * otherwise path evidence decides, resolved per file by EffectiveResolver (human glob > manifest >
    auto-confirmed machine glob); churn on files nobody owns stays unmapped;
  * a PR with no path evidence keeps its non-path allocations (Jira label or auto-confirmed Tier 3).
Only PRs whose allocation actually changes are rewritten.
"""
from __future__ import annotations

from ..core.manifest import Manifest
from .precedence import AUTO_CONFIRM, EffectiveResolver


async def load_resolver(conn, manifest: Manifest) -> EffectiveResolver:
    rows = await conn.fetch("""
        SELECT a.identifier, f.key, m.source::text AS source, m.confidence
          FROM asset_feature_mappings m JOIN code_assets a ON a.id = m.asset_id JOIN features f ON f.id = m.feature_id
         WHERE a.asset_type = 'path_glob' AND m.status = 'confirmed' AND m.source <> 'manifest'""")
    human = [(r["identifier"], r["key"]) for r in rows if r["source"] == "human"]
    machine = [(r["identifier"], r["key"], float(r["confidence"])) for r in rows if r["source"] in ("heuristic", "ai")]
    return EffectiveResolver(manifest, human, machine, AUTO_CONFIRM)


async def reallocate(conn, org_id: str, resolver: EffectiveResolver) -> dict[str, int]:
    fids = {r["key"]: str(r["id"]) for r in await conn.fetch("SELECT id, key FROM features")}
    prs = await conn.fetch("""
        SELECT w.id, w.path_stats, w.is_sweeping,
               coalesce((SELECT jsonb_agg(jsonb_build_object('f', a.feature_id, 's', a.share, 'src', a.source))
                           FROM work_item_allocations a WHERE a.work_item_id = w.id), '[]'::jsonb) AS alloc
          FROM work_items w WHERE w.item_type = 'pr'""")
    decided = {str(r["id"]) for r in await conn.fetch("""
        SELECT w.id FROM work_items w
         WHERE w.item_type = 'pr' AND EXISTS (
               SELECT 1 FROM mapping_suggestions s JOIN work_items x ON x.id = s.subject_id
                WHERE s.subject_type = 'work_item' AND s.decided_at IS NOT NULL AND s.status = 'confirmed'
                  AND (x.id = w.id OR x.external_id = ANY(w.linked_keys)))""")}
    changed, inserts, protected = [], [], 0
    for r in prs:
        if r["is_sweeping"]:
            continue
        current = r["alloc"] or []
        if str(r["id"]) in decided:   # a PR-level human decision: path evidence never overrides it
            protected += 1
            continue
        new = resolver.allocate(r["path_stats"] or [])
        if not new:
            continue
        want = {(fids[k], round(s, 4), src) for k, (s, src) in new.items() if k in fids}
        have = {(str(a["f"]), round(float(a["s"]), 4), a["src"]) for a in current}
        if want != have:
            changed.append(r["id"])
            inserts += [(r["id"], f, org_id, s, src) for f, s, src in want]
    if changed:
        await conn.execute("DELETE FROM work_item_allocations WHERE work_item_id = ANY($1::uuid[])", changed)
        await conn.executemany("""INSERT INTO work_item_allocations (work_item_id, feature_id, org_id, share, source)
                                  VALUES ($1, $2, $3, $4, $5::mapping_source)""", inserts)
    return {"prs": len(prs), "reallocated": len(changed), "protected_human": protected}
