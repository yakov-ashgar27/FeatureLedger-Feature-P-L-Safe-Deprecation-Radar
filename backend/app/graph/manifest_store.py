"""The active manifest: the latest applied `manifest_versions` row, else `features.yaml` on disk.

`record_and_apply` is the single path every manifest change goes through (GitHub push, disk at start-up,
API). It validates first; an invalid manifest is recorded with its structured errors and changes nothing,
so existing mappings keep working until a valid version arrives.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..bootstrap import apply_manifest
from ..config import config
from ..core.manifest import Manifest, ManifestError, parse_manifest


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def active_manifest_text(conn) -> str:
    row = await conn.fetchrow("""SELECT content FROM manifest_versions WHERE status = 'applied' AND content IS NOT NULL
                                 ORDER BY received_at DESC, id DESC LIMIT 1""")
    return row["content"] if row and row["content"] else config.manifest_path.read_text()


async def active_manifest(conn) -> Manifest:
    return parse_manifest(await active_manifest_text(conn))


@dataclass
class ApplyResult:
    status: str                                   # applied | invalid | unchanged
    errors: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    version_id: int | None = None


def validate(text: str) -> tuple[Manifest | None, list[str]]:
    try:
        return parse_manifest(text), []
    except ManifestError as e:
        return None, e.errors


async def record_and_apply(conn, org_id: str, text: str, *, source: str, repo: str | None = None,
                           path: str | None = None, commit_sha: str | None = None) -> ApplyResult:
    manifest, errors = validate(text)
    digest = sha(text)
    if manifest is None:
        vid = await conn.fetchval("""
            INSERT INTO manifest_versions (org_id, source, repo, path, commit_sha, status, errors, content_sha)
            VALUES ($1, $2, $3, $4, $5, 'invalid', $6, $7) RETURNING id""",
            org_id, source, repo, path, commit_sha, errors, digest)
        await conn.execute("""UPDATE integrations SET last_error = $1 WHERE kind = 'github'""",
                           f"features.yaml rejected{f' at {commit_sha[:8]}' if commit_sha else ''}: "
                           f"{len(errors)} error(s); the previous manifest stays active")
        return ApplyResult("invalid", errors, version_id=vid)
    current = await conn.fetchval("""SELECT content_sha FROM manifest_versions WHERE status = 'applied'
                                      ORDER BY received_at DESC, id DESC LIMIT 1""")
    if current == digest:
        vid = await conn.fetchval("""INSERT INTO manifest_versions (org_id, source, repo, path, commit_sha, status,
                                     content_sha) VALUES ($1, $2, $3, $4, $5, 'unchanged', $6) RETURNING id""",
                                  org_id, source, repo, path, commit_sha, digest)
        return ApplyResult("unchanged", version_id=vid)
    summary = await apply_manifest(conn, org_id, manifest)
    vid = await conn.fetchval("""
        INSERT INTO manifest_versions (org_id, source, repo, path, commit_sha, status, content, content_sha, summary)
        VALUES ($1, $2, $3, $4, $5, 'applied', $6, $7, $8) RETURNING id""",
        org_id, source, repo, path, commit_sha, text, digest, summary)
    await conn.execute("""UPDATE integrations SET last_error = NULL
                           WHERE kind = 'github' AND last_error LIKE 'features.yaml rejected%'""")
    return ApplyResult("applied", summary=summary, version_id=vid)
