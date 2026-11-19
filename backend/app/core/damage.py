"""Sunset damage signals: turn Sentry webhooks and generic signal posts into per-feature evidence.

Attribution, most explicit first:
    tag    the event carries a `feature` tag (Sentry tag, or `feature` in a generic signal)
    route  the request route matches one of the feature's `api_routes` in features.yaml
    path   a stack frame's file matches one of the feature's `paths` globs
    none   stored for visibility, blocks nothing

Nothing identifying is kept: titles are redacted (emails, long numbers), routes lose their query string
and numeric / uuid segments, stack frames are only used for matching and never stored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from .manifest import FeatureSpec, glob_specificity, glob_to_regex

FEATURE_TAGS = ("feature", "featureledger.feature", "feature_key")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
NUM_RE = re.compile(r"\+?\d[\d\s().-]{5,}\d")
ID_SEGMENT = re.compile(r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{16,})$")
KINDS = ("error", "deprecated_call")


class SignalError(ValueError):
    pass


@dataclass(frozen=True)
class Signal:
    source: str
    kind: str
    external_id: str
    occurred_at: datetime
    occurrences: int = 1
    title: str | None = None
    route: str | None = None
    feature_tag: str | None = None
    frames: tuple[str, ...] = ()


def redact(s: str | None, limit: int = 200) -> str | None:
    if not s:
        return None
    s = NUM_RE.sub("[number]", EMAIL_RE.sub("[email]", s))
    return " ".join(s.split())[:limit]


def clean_route(raw: str | None) -> str | None:
    if not raw:
        return None
    path = urlparse(raw).path if "://" in raw else raw.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return None
    return "/".join(":id" if ID_SEGMENT.match(seg) else seg for seg in path.split("/"))[:300]


def _route_regex(pattern: str) -> re.Pattern[str]:
    # features.yaml routes: '*' matches anything, including '/', so "/api/v1/export-pdf*" covers sub-paths
    return re.compile("^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$")


def _when(v: Any) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), timezone.utc)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def match_feature(sig: Signal, specs: Iterable[FeatureSpec]) -> tuple[str | None, str]:
    specs = [s for s in specs if not s.shared_overhead]
    keys = {s.key for s in specs}
    if sig.feature_tag and sig.feature_tag in keys:
        return sig.feature_tag, "tag"
    if sig.route:
        best: tuple[int, str] | None = None
        for s in specs:
            for pat in s.api_routes:
                if _route_regex(pat).match(sig.route):
                    score = len(pat.replace("*", ""))
                    if best is None or score > best[0]:
                        best = (score, s.key)
        if best:
            return best[1], "route"
    for frame in sig.frames:
        candidates = [frame.lstrip("./")]
        parts = candidates[0].split("/")
        candidates += ["/".join(parts[i:]) for i in range(1, len(parts) - 1)]   # /app/services/... -> services/...
        best_p: tuple[tuple[int, int], str] | None = None
        for c in candidates:
            for s in specs:
                for g in s.paths:
                    if glob_to_regex(g).match(c):
                        score = glob_specificity(g)
                        if best_p is None or score > best_p[0]:
                            best_p = (score, s.key)
            if best_p:
                return best_p[1], "path"
    return None, "none"


# ------------------------------------------------------------------------------------------ Sentry
def _sentry_tags(event: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in event.get("tags") or []:
        if isinstance(t, (list, tuple)) and len(t) == 2:
            out[str(t[0])] = str(t[1])
        elif isinstance(t, dict) and "key" in t:
            out[str(t["key"])] = str(t.get("value"))
    return out


def _frames(event: dict[str, Any]) -> tuple[str, ...]:
    files: list[str] = []
    for exc in ((event.get("exception") or {}).get("values") or []):
        for fr in reversed(((exc.get("stacktrace") or {}).get("frames") or [])):   # innermost first
            f = fr.get("filename") or fr.get("abs_path")
            if f and fr.get("in_app", True) is not False and f not in files:
                files.append(str(f))
    return tuple(files[:30])


def parse_sentry(resource: str | None, payload: dict[str, Any]) -> list[Signal]:
    """Sentry integration-platform webhooks: `error` and `event_alert` (full event), `issue` (created)."""
    data = payload.get("data") or {}
    event = data.get("error") or data.get("event")
    if event:
        tags = _sentry_tags(event)
        url = (event.get("request") or {}).get("url") or tags.get("url")
        route = clean_route(tags.get("transaction") if (tags.get("transaction") or "").startswith("/") else url)
        eid = event.get("event_id") or event.get("id")
        if not eid:
            raise SignalError("Sentry event without an event_id")
        return [Signal("sentry", "error", f"event:{eid}", _when(event.get("datetime") or event.get("timestamp")),
                       1, redact(event.get("title") or event.get("message")), route,
                       next((tags[k] for k in FEATURE_TAGS if tags.get(k)), None), _frames(event))]
    issue = data.get("issue")
    if issue and (payload.get("action") in (None, "created", "unresolved")):
        if not issue.get("id"):
            raise SignalError("Sentry issue without an id")
        culprit = issue.get("culprit") or ""
        return [Signal("sentry", "error", f"issue:{issue['id']}", _when(issue.get("firstSeen")),
                       max(1, int(issue.get("count") or 1)), redact(issue.get("title")),
                       clean_route(culprit) if culprit.startswith("/") else None, None, ())]
    return []          # installation, comment, resolved issue...: nothing to record


# ----------------------------------------------------------------------------------------- generic
def parse_generic(payload: dict[str, Any]) -> list[Signal]:
    """{"signals": [{"id", "kind": "error"|"deprecated_call", "feature"?, "route"?, "path"?, "count"?,
                     "occurred_at"?, "title"?}]}  - one post may carry up to 500 signals."""
    items = payload.get("signals")
    if not isinstance(items, list) or not items:
        raise SignalError("expected a non-empty 'signals' list")
    if len(items) > 500:
        raise SignalError("at most 500 signals per request")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise SignalError(f"signals[{i}] is not an object")
        kind = it.get("kind")
        if kind not in KINDS:
            raise SignalError(f"signals[{i}].kind must be one of {KINDS}")
        sid = str(it.get("id") or "").strip()
        if not sid or len(sid) > 200:
            raise SignalError(f"signals[{i}].id is required (your own unique id, max 200 chars)")
        try:
            count = max(1, min(int(it.get("count") or 1), 10_000_000))
        except (TypeError, ValueError) as e:
            raise SignalError(f"signals[{i}].count must be a number") from e
        path = it.get("path")
        out.append(Signal("generic", kind, sid, _when(it.get("occurred_at")), count, redact(it.get("title")),
                          clean_route(it.get("route")), (it.get("feature") or None),
                          (str(path),) if path else ()))
    return out
