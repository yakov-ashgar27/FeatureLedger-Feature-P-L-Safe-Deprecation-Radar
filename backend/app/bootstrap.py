"""Real-data mode: create your organization and sync features.yaml into the Feature Graph.

features.yaml is the source of truth for teams, features and Tier 1 mappings. Applying it is
idempotent: teams/features are upserted by key, manifest mappings are replaced, and features
removed from the file are left in place (archive them through Safe Sunset, never by deletion).
"""
from __future__ import annotations

from datetime import date

from .core.manifest import Manifest


async def ensure_org(conn, slug: str, name: str) -> str:
    org = await conn.fetchval("SELECT id FROM organizations WHERE slug = $1", slug)
    if org is None:
        org = await conn.fetchval("INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id", name, slug)
    await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org))
    await conn.execute("INSERT INTO org_settings (org_id) VALUES ($1) ON CONFLICT (org_id) DO NOTHING", org)
    return str(org)


async def apply_manifest(conn, org_id: str, manifest: Manifest) -> dict[str, int]:
    """Run inside a transaction where app.org_id = org_id."""
    for t in manifest.teams:
        await conn.execute("""
            INSERT INTO teams (org_id, key, name, headcount) VALUES ($1, $2, $3, $4)
            ON CONFLICT (org_id, key) DO UPDATE SET name = EXCLUDED.name, headcount = EXCLUDED.headcount""",
            org_id, t.key, t.name, t.headcount)
    teams = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM teams")}
    missing_teams = sorted({f.owner_team for f in manifest.features if f.owner_team and f.owner_team not in teams})
    for key in missing_teams:  # declared only as owner_team: create with headcount 0 and let the user fix it
        teams[key] = await conn.fetchval(
            "INSERT INTO teams (org_id, key, name, headcount) VALUES ($1, $2, $2, 0) RETURNING id", org_id, key)

    fids: dict[str, object] = {}
    for f in manifest.features:
        meta = {"replaced_by": f.replaced_by} if f.replaced_by else {}
        fids[f.key] = await conn.fetchval("""
            INSERT INTO features (org_id, key, name, description, owner_team_id, flag_key, tier_gate,
                                  is_shared_overhead, allocation_rule, allocation_weights, is_regulatory,
                                  launched_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::allocation_rule, $10, $11, $12, $13)
            ON CONFLICT (org_id, key) DO UPDATE SET
                name = EXCLUDED.name, description = EXCLUDED.description, owner_team_id = EXCLUDED.owner_team_id,
                flag_key = EXCLUDED.flag_key, tier_gate = EXCLUDED.tier_gate,
                is_shared_overhead = EXCLUDED.is_shared_overhead, allocation_rule = EXCLUDED.allocation_rule,
                allocation_weights = EXCLUDED.allocation_weights, is_regulatory = EXCLUDED.is_regulatory,
                launched_at = EXCLUDED.launched_at, metadata = features.metadata || EXCLUDED.metadata,
                updated_at = now()
            RETURNING id""",
            org_id, f.key, f.name, f.description, teams.get(f.owner_team) if f.owner_team else None, f.flag_key,
            f.tier_gate, f.shared_overhead, f.allocation_rule, f.allocation_weights, f.regulatory,
            date.fromisoformat(f.launched_at) if f.launched_at else None, meta)

    await conn.execute("""DELETE FROM asset_feature_mappings WHERE source = 'manifest'
                          AND feature_id = ANY($1::uuid[])""", list(fids.values()))
    mapped = 0
    for f in manifest.features:
        for asset_type, values in (("path_glob", f.paths), ("api_route", f.api_routes),
                                   ("analytics_event", f.events + f.event_aliases),
                                   ("feature_flag", (f.flag_key,) if f.flag_key else ()),
                                   ("jira_label", f.jira_labels), ("jira_epic", f.jira_epics),
                                   ("support_tag", f.support_tags), ("cost_tag", f.cost_tags)):
            for v in values:
                aid = await conn.fetchval("""
                    INSERT INTO code_assets (org_id, asset_type, identifier) VALUES ($1, $2::asset_type, $3)
                    ON CONFLICT (org_id, repo_id, asset_type, identifier) DO UPDATE SET last_seen = now()
                    RETURNING id""", org_id, asset_type, v)
                await conn.execute("""
                    INSERT INTO asset_feature_mappings (asset_id, feature_id, org_id, source, confidence, status)
                    VALUES ($1, $2, $3, 'manifest', 1, 'confirmed')
                    ON CONFLICT (asset_id, feature_id) DO UPDATE SET source = 'manifest', confidence = 1,
                        status = 'confirmed'""", aid, fids[f.key], org_id)
                mapped += 1
    return {"teams": len(teams), "teams_without_headcount": len(missing_teams), "features": len(fids),
            "mappings": mapped}
