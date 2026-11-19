"""features.yaml draft generator: folders -> features, routes / events / flags from code, owners from CODEOWNERS."""
from pathlib import Path

import pytest

from app.core.manifest import parse_manifest
from app.tools.draft_manifest import draft, main, route_prefix, slug

FILES = {
    "CODEOWNERS": "* @acme/platform\n/services/billing/ @acme/billing-team\n/web/src/features/billing/ @acme/billing-team\n"
                  "/services/legacyExport/ @acme/docs @someone\n",
    "services/billing/api.py": '@router.get("/api/v1/invoices/{id}")\ndef g(): flags.get("billing-v2")\n'
                               '@router.post("/api/v1/invoices")\ndef p(): analytics.track("invoice_created")\n',
    "web/src/features/billing/Page.tsx": 'const on = useFlag("billing-v2"); posthog.capture("invoice_viewed");\n',
    "services/legacyExport/render.go": 'mux.HandleFunc("/export/pdf/v1", h)\nif ld.BoolVariation("legacy-export", u, false) {}\n',
    "services/common/db.py": "x = 1\n",
    "services/integrations/zapier_v1/hooks.ts": "app.post('/hooks/v1/zapier/:id', h); track('zap_fired');\n",
    "node_modules/pkg/services/evil/index.js": "track('should_not_be_seen')\n",
    "services/billing/__tests__/fixture.json": "{}",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_draft_is_valid_and_finds_features_routes_events_flags_owners(repo):
    text, notes = draft(repo)
    m = parse_manifest(text)
    f = m.by_key()
    assert set(f) == {"billing", "legacy-export", "zapier-v1", "platform"}
    b = f["billing"]
    assert set(b.paths) == {"services/billing/**", "web/src/features/billing/**"}      # merged across front and back
    assert b.owner_team == "billing-team" and b.flag_key == "billing-v2"
    assert b.api_routes == ("/api/v1/invoices*",)
    assert set(b.events) == {"invoice_created", "invoice_viewed"}
    assert f["legacy-export"].flag_key == "legacy-export" and f["legacy-export"].api_routes == ("/export/pdf/v1*",)
    assert f["legacy-export"].owner_team == "docs"                                       # individuals are ignored
    assert f["zapier-v1"].api_routes == ("/hooks/v1/zapier*",) and f["zapier-v1"].owner_team == "platform"
    assert f["platform"].shared_overhead and f["platform"].allocation_rule == "proportional_usage"
    assert "should_not_be_seen" not in text and "node_modules" not in text
    assert {t.key for t in m.teams} == {"billing-team", "docs", "platform"} and all(t.headcount == 0 for t in m.teams)
    assert "the draft is valid" in notes[0]


def test_helpers():
    assert slug("legacyExport") == "legacy-export" and slug("zapier_v1") == "zapier-v1" and slug("X") == "x-feature"
    assert route_prefix("/api/v1/documents/{id}/share") == "/api/v1/documents*"
    assert route_prefix("^api/v1/reports/") == "/api/v1/reports*"
    assert route_prefix("/api/v1") is None and route_prefix("/:id") is None


def test_cli_refuses_to_overwrite_and_explains_unknown_layouts(repo, tmp_path, capsys):
    out = tmp_path / "features.yaml"
    assert main([str(repo), "-o", str(out)]) == 0 and out.read_text().startswith("# DRAFT")
    with pytest.raises(SystemExit):
        main([str(repo), "-o", str(out)])
    empty = tmp_path / "flat"
    (empty / "src").mkdir(parents=True)
    (empty / "src" / "main.py").write_text("print(1)\n")
    with pytest.raises(SystemExit) as e:
        draft(empty)
    assert "--container" in str(e.value)
    (empty / "src" / "orders").mkdir()
    (empty / "src" / "orders" / "views.py").write_text('path("orders/<int:pk>/", v)\n')
    text, _ = draft(empty, ("src",))
    assert parse_manifest(text).by_key()["orders"].api_routes == ("/orders*",)
