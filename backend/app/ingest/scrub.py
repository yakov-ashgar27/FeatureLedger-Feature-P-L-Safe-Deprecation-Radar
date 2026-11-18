"""Privacy scrubber: an ALLOWLIST, not a denylist.

Anything not explicitly listed is dropped before an envelope is persisted, so a new field added by
GitHub (e.g. a future `author_association_v2`) can never leak individual identity into our store.
"""
from __future__ import annotations

import re
from typing import Any

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")

PR_ALLOWED = {"number", "title", "state", "created_at", "updated_at", "merged_at", "closed_at",
              "additions", "deletions", "changed_files", "draft"}
FILE_ALLOWED = {"filename", "additions", "deletions", "status", "previous_filename"}


def scrub_pr(pr: dict[str, Any]) -> dict[str, Any]:
    out = {k: pr[k] for k in PR_ALLOWED if k in pr}
    out["labels"] = sorted({lbl["name"] for lbl in pr.get("labels") or [] if isinstance(lbl, dict) and "name" in lbl})
    # keep only issue keys from the free-text body, never the text itself
    body = pr.get("body") or ""
    out["linked_keys"] = sorted(set(ISSUE_KEY_RE.findall(f"{pr.get('title', '')} {body}")))
    return out


def scrub_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: f[k] for k in FILE_ALLOWED if k in f} for f in files]


def scrub_reviews(reviews: list[dict[str, Any]]) -> dict[str, int]:
    """Reduce reviews to counts. Reviewer identity is discarded."""
    states = [r.get("state") for r in reviews]
    return {"changes_requested": states.count("CHANGES_REQUESTED"), "approved": states.count("APPROVED"),
            "total": len(states)}
