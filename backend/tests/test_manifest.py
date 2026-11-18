import pytest

from app.core.manifest import ManifestError, glob_to_regex, parse_manifest

GOOD = """
version: 1
ignore: ["docs/**"]
features:
  - key: legacy-pdf-export
    name: Legacy PDF Exporter
    owner_team: docs-platform
    paths: ["services/export/**", "web/src/features/export-pdf/**"]
    api_routes: ["/api/v1/export-pdf*"]
    events: [pdf_exported]
    event_aliases: [export_pdf_clicked]
    flag_key: legacy-pdf-export
  - key: pdf-export-v2
    name: PDF Export v2
    paths: ["services/export/v2/**"]
    api_routes: ["/api/v1/export*", "/api/v2/export*"]
  - key: platform-auth
    name: Auth
    paths: ["services/auth/**"]
    shared_overhead: true
    allocation_rule: proportional_usage
"""


def test_glob_semantics():
    assert glob_to_regex("services/**/*.py").match("services/a/b/c.py")
    assert glob_to_regex("services/**/*.py").match("services/c.py")
    assert not glob_to_regex("services/*.py").match("services/a/c.py")
    assert glob_to_regex("**/package-lock.json").match("package-lock.json")
    assert glob_to_regex("web/?.ts").match("web/a.ts")


def test_most_specific_glob_wins():
    m = parse_manifest(GOOD)
    assert m.owner_of("services/export/v2/render.py") == "pdf-export-v2"
    assert m.owner_of("services/export/v1/render.py") == "legacy-pdf-export"
    assert m.owner_of("services/billing/x.py") is None
    assert m.owner_of("docs/readme.md") is None
    assert m.owner_of("web/package-lock.json") is None


def test_routes_and_events():
    m = parse_manifest(GOOD)
    assert m.feature_for_route("/api/v1/export-pdf/123") == "legacy-pdf-export"  # longer prefix wins
    assert m.feature_for_route("/api/v1/exports") == "pdf-export-v2"
    assert m.feature_for_event("export_pdf_clicked") == "legacy-pdf-export"


def test_allocate_paths_ignores_lockfiles_and_leaves_unmapped_remainder():
    m = parse_manifest(GOOD)
    shares, churn = m.allocate_paths([
        {"path": "services/export/v1/a.py", "additions": 60, "deletions": 0},
        {"path": "services/export/v2/b.py", "additions": 20, "deletions": 40},
        {"path": "services/billing/c.py", "additions": 40, "deletions": 0},
        {"path": "package-lock.json", "additions": 5000, "deletions": 4000},
    ])
    assert churn == 140
    assert shares == {"legacy-pdf-export": pytest.approx(60 / 140, abs=1e-5),
                      "pdf-export-v2": pytest.approx(40 / 140, abs=1e-5)}


@pytest.mark.parametrize("bad, msg", [
    ("version: 2\nfeatures: [{key: ab, name: A}]", "unsupported manifest version"),
    ("version: 1\nfeatures: [{key: Bad_Key, name: A}]", "'key' must match"),
    ("version: 1\nfeatures: [{key: ab, name: A}, {key: ab, name: B}]", "duplicate key"),
    ("version: 1\nfeatures: [{key: ab, name: A, allocation_rule: proportional_cost}]", "requires shared_overhead"),
    ("version: 1\nfeatures: [{key: ab, name: A, paths: [x/**]}, {key: cd, name: C, paths: [x/**]}]", "claimed by both"),
    ("version: 1\nfeatures: [{key: ab, name: A, api_routes: [api/x]}]", "must start with '/'"),
    ("version: 1\nfeatures: [{key: ab, name: A, tier_gate: platinum}]", "tier_gate"),
    ("version: 1\nfeatures: [{key: ab, name: A, shared_overhead: true, allocation_rule: fixed_weights,"
     " allocation_weights: {zz: 1}}]", "unknown feature"),
    ("version: 1\nfeatures: [", "invalid YAML"),
])
def test_validation_errors(bad, msg):
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert msg in str(e.value)
