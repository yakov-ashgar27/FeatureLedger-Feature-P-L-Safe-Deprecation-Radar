"""What a cleanup PR changes, computed purely (no network) so it is identical in sandbox and live mode.

    delete    every file under the feature's manifest paths that the manifest attributes to it
    edit      features.yaml: the feature's entry removed (validated: the result must still parse)
    add       .featureledger/sunset/<key>.json: the structured cleanup patch for what a robot must not
              edit blindly: call sites importing the feature, flag checks, API routes, analytics events
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.manifest import FeatureSpec, Manifest, ManifestError, glob_to_regex, parse_manifest


class CleanupError(ValueError):
    pass


@dataclass
class CleanupChange:
    feature_key: str
    branch: str
    title: str
    body: str
    deletions: list[str]
    manifest_path: str
    manifest_after: str | None
    patch_path: str
    patch: dict[str, Any]
    files_edited: list[str] = field(default_factory=list)


def branch_for(key: str) -> str:
    return f"sunset/{key}"


def remove_feature_block(text: str, key: str) -> str:
    """Remove one `- key: <key>` item (block or flow style) from features.yaml, keeping every other
    byte (comments, ordering, formatting) intact. Raises CleanupError if the result is not valid."""
    lines = text.splitlines(keepends=True)
    item = re.compile(r"^(\s*)-\s+(?:key:\s*['\"]?" + re.escape(key) + r"['\"]?\s*(?:#.*)?$|\{.*\bkey:\s*['\"]?"
                      + re.escape(key) + r"['\"]?\s*[,}])")
    start = next((i for i, ln in enumerate(lines) if item.match(ln.rstrip("\n"))), None)
    if start is None:
        raise CleanupError(f"{key} not found in the manifest")
    indent = len(item.match(lines[start].rstrip("\n")).group(1))
    end = start + 1
    if "{" not in lines[start]:
        while end < len(lines):
            ln = lines[end]
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
                break
            end += 1
        # trailing blank lines belong to the separation before the next item: keep exactly one
        while end - 1 > start and not lines[end - 1].strip():
            end -= 1
        if end < len(lines) and not lines[end].strip():
            end += 1
    out = "".join(lines[:start] + lines[end:])
    try:
        m = parse_manifest(out)
    except ManifestError as e:
        raise CleanupError(f"removing {key} leaves an invalid manifest: {e.errors}") from e
    if key in m.by_key():
        raise CleanupError(f"{key} is still present after removal")
    return out


def owned_files(spec: FeatureSpec, manifest: Manifest, files: list[str]) -> list[str]:
    regs = [glob_to_regex(g) for g in spec.paths]
    out = []
    for p in sorted(set(files)):
        if any(r.match(p) for r in regs) and manifest.owner_of(p) in (spec.key, None):
            out.append(p)
    return out


def _flag_check_hints(spec: FeatureSpec) -> list[str]:
    if not spec.flag_key:
        return []
    k = spec.flag_key
    return [f'variation("{k}"', f"isEnabled('{k}'", f'checkGate("{k}"', f'useFlag("{k}"', f'"{k}"']


def build_cleanup_change(*, spec: FeatureSpec, manifest: Manifest, manifest_text: str, manifest_path: str,
                         repo_files: list[str], blast_radius: dict[str, Any] | None, plan: dict[str, Any],
                         audit: list[dict[str, Any]], scream: dict[str, Any]) -> CleanupChange:
    deletions = owned_files(spec, manifest, repo_files)
    br = blast_radius or {}
    inbound = br.get("inbound_references", [])
    callers = sorted({r["from_path"] for r in inbound} - set(deletions))
    try:
        manifest_after = remove_feature_block(manifest_text, spec.key)
    except CleanupError:
        manifest_after = None     # reported in the PR body; humans fix the manifest by hand
    patch = {
        "schema": "featureledger.sunset-cleanup/v1",
        "feature": {"key": spec.key, "name": spec.name, "replaced_by": spec.replaced_by},
        "plan_id": plan["id"],
        "flag": {"key": spec.flag_key, "final_state": "fully_off", "search_for": _flag_check_hints(spec),
                 "action": "remove every check, keep the off-branch, then archive the flag in the provider"}
        if spec.flag_key else None,
        "routes_to_remove": list(spec.api_routes),
        "events_to_retire": list(spec.events) + list(spec.event_aliases),
        "call_sites_to_edit": [{"path": r["from_path"], "imports": r.get("imports", []),
                                "owner_feature": r.get("owner_feature"),
                                "acknowledged": bool(r.get("acknowledged"))} for r in inbound
                               if r["from_path"] in callers],
        "files_deleted": deletions,
        "audit_chain_head": audit[-1]["hash"] if audit else None,
        "scream_test": scream,
    }
    body = render_body(spec, plan, br, deletions, callers, manifest_after is not None, audit, scream, patch)
    return CleanupChange(
        feature_key=spec.key, branch=branch_for(spec.key),
        title=f"chore(sunset): remove {spec.name} ({spec.key})", body=body, deletions=deletions,
        manifest_path=manifest_path, manifest_after=manifest_after,
        patch_path=f".featureledger/sunset/{spec.key}.json", patch=patch,
        files_edited=[manifest_path] if manifest_after is not None else [])


def render_body(spec, plan, br, deletions, callers, manifest_ok, audit, scream, patch) -> str:
    def cell(v: Any) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    lines = [
        f"## Sunset: {spec.name} (`{spec.key}`)",
        "",
        "Opened automatically by **FeatureLedger Safe Sunset** after a clean scream test. "
        "**Merging this PR archives the feature in FeatureLedger.** Closing it, or a rollback, "
        "turns the flag back on and returns the plan to customer outreach.",
        "",
        f"- Reason: {cell(plan.get('reason', ''))}",
        f"- Plan: `{plan['id']}` · rollbacks so far: {plan.get('rollback_count', 0)}",
        f"- Replaced by: `{spec.replaced_by}`" if spec.replaced_by else "- Replaced by: (none)",
        "",
        "### Scream test",
        "| Flag | Fully off since | Quiet days | Required | 'on' evaluations | Off evaluations | Tickets |",
        "|---|---|---|---|---|---|---|",
        f"| `{spec.flag_key}` | {scream.get('fully_off_at', '?')} | {scream.get('quiet_days', '?')} | "
        f"{scream.get('required_days', '?')} | {scream.get('flag_on_evaluations', 0)} | "
        f"{scream.get('flag_off_evaluations', 0)} | {scream.get('tickets_since_off', 0)} |",
        "",
        "### Blast radius",
        f"- Inbound references: **{len(br.get('inbound_references', []))}** "
        f"({sum(1 for r in br.get('inbound_references', []) if r.get('acknowledged'))} acknowledged by their owners)",
    ]
    for r in br.get("inbound_references", [])[:50]:
        imports = ", ".join(f"`{x}`" for x in r.get("imports", []))
        lines.append(f"  - `{r['from_path']}` ({r.get('owner_feature', 'unmapped')}) imports {imports}"
                     f"{' ✓ acknowledged' if r.get('acknowledged') else ''}")
    if br.get("by_owner_feature"):
        lines.append("- By owning feature: " + ", ".join(f"`{k}` {v}" for k, v in br["by_owner_feature"].items()))
    lines.append(f"- Risk: **{br.get('risk', 'unknown')}**")
    lines += ["", f"### Deleted in this PR ({len(deletions)} files)"]
    lines += [f"- `{p}`" for p in deletions[:200]] or ["- (no files found under the feature's paths)"]
    if len(deletions) > 200:
        lines.append(f"- … and {len(deletions) - 200} more")
    lines += ["", "### Edited in this PR",
              f"- `features.yaml`: `{spec.key}` entry removed" if manifest_ok
              else "- ⚠️ `features.yaml` could not be edited automatically; remove the entry by hand",
              f"- `.featureledger/sunset/{spec.key}.json`: structured cleanup patch (below)",
              "", "### Still to do by hand (review checklist)"]
    lines += [f"- [ ] remove imports / dead branches in `{p}`" for p in callers] or ["- [x] no call sites outside the feature"]
    if spec.flag_key:
        lines.append(f"- [ ] remove every `{spec.flag_key}` flag check (keep the off branch), then archive the flag")
    lines += [f"- [ ] remove route `{r}` and its OpenAPI entry" for r in spec.api_routes]
    if patch["events_to_retire"]:
        lines.append(f"- [ ] retire analytics events: {', '.join(f'`{e}`' for e in patch['events_to_retire'])}")
    lines += ["", "### Audit trail (hash-chained, append-only)",
              "| # | When (UTC) | Transition | Action | By |", "|---|---|---|---|---|"]
    for i, a in enumerate(audit, 1):
        frm = a.get("from_state") or "∅"
        lines.append(f"| {i} | {str(a.get('created_at', ''))[:16]} | {frm} → {a['to_state']} | {a['action']} | "
                     f"{cell((a.get('payload') or {}).get('actor_role', 'system'))} |")
    if audit:
        lines.append(f"\nChain head: `{audit[-1]['hash']}`")
    lines += ["", "<details><summary>Structured cleanup patch</summary>", "", "```json",
              json.dumps(patch, indent=2, default=str), "```", "</details>"]
    return "\n".join(lines)
