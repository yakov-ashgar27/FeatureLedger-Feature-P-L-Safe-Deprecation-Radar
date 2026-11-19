"""Draft a features.yaml from a local checkout of your repository.

    python -m app.tools.draft_manifest ../my-repo                 # prints the draft
    python -m app.tools.draft_manifest ../my-repo -o features.yaml
    make draft-manifest REPO=../my-repo

Reads files only; nothing leaves your machine. What it finds:

    features  child folders of the usual feature containers (services/, features/, modules/, apps/,
              packages/, domains/, plugins/, integrations/; add your own with --container). The same name
              under several containers (web/src/features/editor + services/editor) becomes one feature.
              Shared folders (common, shared, core, utils, lib...) go into one 'platform' entry.
    routes    route declarations (FastAPI/Flask, Express/Nest, Django, Spring, Go net/http, Rails), as prefixes
    events    analytics calls (track / capture / logEvent) with a literal event name
    flags     feature-flag lookups (LaunchDarkly, Statsig, Unleash, GrowthBook, Flagsmith, generic isEnabled)
    owners    CODEOWNERS team handles (@org/team), last matching rule wins, as in GitHub

It is a draft: every guess is marked, team headcounts are left at 0 for you to fill in, and the result is
validated with the same parser the API uses before it is written.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..core.manifest import ManifestError, glob_to_regex, parse_manifest

CONTAINERS = {"services", "features", "modules", "apps", "packages", "domains", "plugins", "integrations", "extensions"}
SHARED = {"common", "shared", "core", "utils", "util", "lib", "libs", "helpers", "types", "config", "configs",
          "infra", "platform", "base", "internal", "ui", "design-system", "testing", "test", "tests", "__tests__",
          "mocks", "fixtures", "scripts", "migrations", "generated", "static", "assets"}
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".next", "venv", ".venv", "__pycache__", "target",
             "coverage", ".tox", ".mypy_cache", ".pytest_cache", ".turbo", ".cache", "out", "bin", "obj"}
SOURCE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rb", ".java", ".kt", ".cs", ".php", ".rs", ".swift"}
MAX_FILES, MAX_BYTES = 60_000, 512_000

ROUTE_RES = [
    re.compile(r"""@(?:\w+\.)?(?:get|post|put|patch|delete|route|api_route|websocket)\(\s*['"](/[^'"\s]*)['"]"""),  # FastAPI/Flask
    re.compile(r"""\b(?:app|router|r|api|server)\.(?:get|post|put|patch|delete|all|use|route)\(\s*['"`](/[^'"`\s]*)['"`]"""),  # Express
    re.compile(r"""@(?:Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?['"](/[^'"\s]*)['"]"""),  # Spring
    re.compile(r"""@(?:Controller|Get|Post|Put|Patch|Delete)\(\s*['"](/?[^'"\s]+)['"]"""),  # NestJS
    re.compile(r"""\b(?:re_)?path\(\s*r?['"]\^?(/?[^'"\s]+)['"]"""),  # Django
    re.compile(r"""\.(?:HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Get|Post)\(\s*"(/[^"\s]*)\""""),  # Go
    re.compile(r"""^\s*(?:get|post|put|patch|delete|resources?)\s+['"](/?[^'"\s]+)['"]""", re.M),  # Rails
]
EVENT_RES = [
    re.compile(r"""\b(?:analytics|posthog|mixpanel|amplitude|segment|rudderanalytics|telemetry)\s*\.\s*(?:track|capture|logEvent)\(\s*['"`]([A-Za-z][\w .:-]{1,79})['"`]"""),
    re.compile(r"""\b(?:track|trackEvent|captureEvent|logEvent)\(\s*['"`]([A-Za-z][\w.:-]{1,79})['"`]"""),
    re.compile(r"""\bcapture\(\s*(?:distinct_id\s*=\s*)?[\w.\[\]'"]+\s*,\s*(?:event\s*=\s*)?['"]([A-Za-z][\w.:-]{1,79})['"]"""),
]
FLAG_RES = [
    re.compile(r"""\b(?:[Bb]ool|[Ss]tring|[Ii]nt|[Ff]loat|[Jj]son|JSON)?[Vv]ariation(?:Detail)?\(\s*['"`]([\w.-]{2,80})['"`]"""),
    re.compile(r"""\b(?:checkGate|check_gate|getFeatureGate|useGate|useFeatureGate|getExperiment)\(\s*(?:[\w.]+\s*,\s*)?['"`]([\w.-]{2,80})['"`]"""),
    re.compile(r"""\b(?:isEnabled|is_enabled|isFeatureEnabled|is_feature_enabled|useFlag|useFeatureFlag|useFeatureIsOn|isOn|is_on|feature_enabled)\(\s*['"`]([\w.-]{2,80})['"`]"""),
    re.compile(r"""\b(?:flags|featureFlags|feature_flags)\s*(?:\.get|\[)\(?\s*['"`]([\w.-]{2,80})['"`]"""),
]
GENERIC_SEGMENTS = {"api", "rest", "public", "internal", "graphql", "rpc"}
CODEOWNERS_FILES = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()).strip("-")
    s = re.sub(r"-v(\d+)$", r"-v\1", s)
    return (s if len(s) >= 2 else f"{s}-feature")[:80]


def title(key: str) -> str:
    return " ".join(w.upper() if re.fullmatch(r"v\d+|ai|api|pdf|sso|ui|ux|id|sdk", w) else w.capitalize()
                    for w in key.split("-"))


def route_prefix(route: str) -> str | None:
    r = "/" + route.lstrip("^/").split("?")[0]
    out = []
    for seg in r.strip("/").split("/"):
        if not seg or re.match(r"^[{:<(*\[$]|.*[{<(]", seg) or re.fullmatch(r"\d+", seg):
            break
        out.append(seg)
        if len(out) == 3:
            break
    if all(seg in GENERIC_SEGMENTS or re.fullmatch(r"v\d+", seg) for seg in out):
        return None                     # "/api/v1*" would claim every call
    return "/" + "/".join(out) + "*"


@dataclass
class Draft:
    key: str
    dirs: list[str] = field(default_factory=list)
    files: int = 0
    routes: Counter = field(default_factory=Counter)
    events: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)
    owners: Counter = field(default_factory=Counter)
    shared: bool = False


def walk(root: Path) -> list[str]:
    files: list[str] = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        rel = os.path.relpath(d, root)
        for n in sorted(names):
            files.append(n if rel == "." else f"{rel}/{n}".replace(os.sep, "/"))
            if len(files) >= MAX_FILES:
                return files
    return files


def feature_dirs(files: list[str], containers: set[str]) -> dict[str, tuple[str, bool]]:
    """dir -> (feature key, is_shared). A dir is a feature if its parent is a container and it is not one."""
    out: dict[str, tuple[str, bool]] = {}
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts)):
            parent, name = parts[i - 1].lower(), parts[i]
            if parent in containers and name.lower() not in containers:
                d = "/".join(parts[: i + 1])
                if d not in out:
                    shared = name.lower() in SHARED
                    out[d] = ("platform" if shared else slug(name), shared)
                break
    return out


def codeowners(root: Path) -> list[tuple[re.Pattern[str], list[str]]]:
    for name in CODEOWNERS_FILES:
        p = root / name
        if p.is_file():
            rules = []
            for line in p.read_text(errors="replace").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                pat, *owners = line.split()
                teams = [o.split("/", 1)[1] for o in owners if o.startswith("@") and "/" in o]
                g = pat.lstrip("/") if pat.startswith("/") else "**/" + pat
                g = g + "**" if g.endswith("/") else g
                if not re.search(r"[*?]", pat.rstrip("/").split("/")[-1]) and not pat.endswith("/"):
                    g = g + "/**"          # a bare name matches a directory and everything under it
                rules.append((glob_to_regex(g), [slug(t) for t in teams]))
            return rules
    return []


def owner_of(path: str, rules: list[tuple[re.Pattern[str], list[str]]]) -> list[str]:
    hit: list[str] = []
    for rx, teams in rules:
        if rx.match(path):
            hit = teams
    return hit


def draft(root: Path, extra_containers: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """-> (features.yaml text, notes for the person)."""
    containers = CONTAINERS | {c.lower() for c in extra_containers}
    files = walk(root)
    fdirs = feature_dirs(files, containers)
    notes: list[str] = []
    if not fdirs:
        raise SystemExit("no feature folders found (looked for children of: " + ", ".join(sorted(containers)) +
                         "). Pass --container <folder name> for your layout.")
    drafts: dict[str, Draft] = {}
    for d, (key, shared) in sorted(fdirs.items()):
        dr = drafts.setdefault(key, Draft(key, shared=shared))
        dr.dirs.append(d)
    owners = codeowners(root)
    by_len = sorted(fdirs, key=len, reverse=True)
    for f in files:
        d = next((x for x in by_len if f.startswith(x + "/")), None)
        if d is None:
            continue
        dr = drafts[fdirs[d][0]]
        dr.files += 1
        for t in owner_of(f, owners):
            dr.owners[t] += 1
        p = root / f
        if p.suffix not in SOURCE_EXT:
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for rx in ROUTE_RES:
            for m in rx.finditer(text):
                pre = route_prefix(m.group(1))
                if pre:
                    dr.routes[pre] += 1
        for rx in EVENT_RES:
            for m in rx.finditer(text):
                dr.events[m.group(1)] += 1
        for rx in FLAG_RES:
            for m in rx.finditer(text):
                dr.flags[m.group(1)] += 1

    # one owner per event / flag / route prefix: the feature that uses it most
    def assign(attr: str) -> dict[str, str]:
        best: dict[str, tuple[int, str]] = {}
        for dr in drafts.values():
            for k, n in getattr(dr, attr).items():
                if k not in best or n > best[k][0]:
                    best[k] = (n, dr.key)
        return {k: v[1] for k, v in best.items()}
    ev_owner, fl_owner, rt_owner = assign("events"), assign("flags"), assign("routes")

    teams = sorted({t for dr in drafts.values() for t in dr.owners})
    lines = [
        "# DRAFT generated by `python -m app.tools.draft_manifest` from " + str(root.name or root),
        "# Review every entry: names, owners, routes, events and flags are guesses from the code.",
        "# Validate: curl -X POST localhost:8000/api/v1/manifest/validate --data-binary @features.yaml",
        "version: 1",
        "",
    ]
    if teams:
        lines += ["# From CODEOWNERS. Set each team's real headcount (engineers): it calibrates engineering cost.",
                  "teams:"]
        lines += [f"  - {{ key: {t}, name: {title(t)}, headcount: 0 }}   # TODO headcount" for t in teams]
        notes.append(f"{len(teams)} team(s) from CODEOWNERS: fill in their headcount")
    else:
        lines += ["# No CODEOWNERS teams found. Add your teams (key, name, headcount) and set owner_team below.",
                  "teams: []"]
    lines += ["", "ignore:", '  - "**/fixtures/**"', '  - "**/__tests__/**"', '  - "**/*.test.*"', "", "features:"]

    for dr in sorted(drafts.values(), key=lambda x: (x.shared, x.key)):
        lines.append(f"  - key: {dr.key}")
        lines.append(f"    name: {title(dr.key)}" + ("   # shared code every feature relies on" if dr.shared else ""))
        owner = dr.owners.most_common(1)[0][0] if dr.owners else None
        lines.append(f"    owner_team: {owner}" if owner else "    # owner_team: TODO")
        lines.append("    paths: [" + ", ".join(f'"{d}/**"' for d in sorted(dr.dirs)) + f"]   # {dr.files} file(s)")
        if dr.shared:
            lines += ["    shared_overhead: true", "    allocation_rule: proportional_usage"]
            continue
        routes = sorted(r for r in dr.routes if rt_owner[r] == dr.key)
        if routes:
            lines.append("    api_routes: [" + ", ".join(f'"{r}"' for r in routes[:12]) + "]")
        events = [e for e, _ in dr.events.most_common() if ev_owner[e] == dr.key][:12]
        if events:
            lines.append("    events: [" + ", ".join(f'"{e}"' for e in events) + "]")
        flags = [f for f, _ in dr.flags.most_common() if fl_owner[f] == dr.key]
        if flags:
            lines.append(f'    flag_key: "{flags[0]}"' + (f"   # also seen here: {', '.join(flags[1:4])}" if len(flags) > 1 else ""))
        else:
            lines.append("    # flag_key: TODO (needed for a reversible Safe Sunset)")
    text = "\n".join(lines) + "\n"

    try:
        m = parse_manifest(text)
        notes.insert(0, f"{len(m.features)} feature(s) drafted from {len(files)} file(s); the draft is valid")
    except ManifestError as e:
        notes.insert(0, "the draft does not validate yet: " + "; ".join(e.errors[:5]))
    missing_owner = sum(1 for d in drafts.values() if not d.owners and not d.shared)
    if missing_owner:
        notes.append(f"{missing_owner} feature(s) have no owner_team")
    no_flag = sum(1 for d in drafts.values() if not d.flags and not d.shared)
    if no_flag:
        notes.append(f"{no_flag} feature(s) have no flag_key")
    return text, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draft a features.yaml from a local repository checkout.")
    ap.add_argument("repo", type=Path, help="path to a local checkout")
    ap.add_argument("-o", "--out", type=Path, help="write here instead of printing")
    ap.add_argument("--container", action="append", default=[], help="another folder name whose children are features")
    ap.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    a = ap.parse_args(argv)
    if not a.repo.is_dir():
        ap.error(f"{a.repo} is not a directory")
    text, notes = draft(a.repo.resolve(), tuple(a.container))
    if a.out:
        if a.out.exists() and not a.force:
            ap.error(f"{a.out} exists; pass --force to overwrite (or choose another file)")
        a.out.write_text(text)
    else:
        sys.stdout.write(text)
    for n in notes:
        print(f"• {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
