from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.manifest import parse_manifest
from app.core.simulate import simulate, trailing_months
from app.core.pnl import FeaturePnl
from app.core.sunset import PlanView, SunsetState, TransitionError, advance, cancel, rollback
from app.core.sunset_artifacts import DossierAccount, blast_radius, cleanup_pr_outline, impact_dossier
from app.core.types import OrgSettings

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
MANIFEST = parse_manifest("""
version: 1
features:
  - {key: legacy-pdf-export, name: Legacy PDF Exporter, paths: ["services/export/pdf_v1/**"],
     api_routes: ["/api/v1/export-pdf*"], flag_key: legacy-pdf-export}
  - {key: editor, name: Editor, paths: ["services/editor/**"]}
""")
SPEC = MANIFEST.by_key()["legacy-pdf-export"]


def plan(state, **kw):
    base = dict(state=state, feature_is_regulatory=False, flag_key="legacy-pdf-export",
                scream_test_days=30, state_entered_at=T0)
    base.update(kw)
    return PlanView(**base)


def test_blast_radius_and_cleanup_pr():
    edges = [("services/editor/toolbar.py", "services/export/pdf_v1/api.py"),
             ("services/export/pdf_v1/api.py", "services/export/pdf_v1/render.py"),
             ("scripts/nightly.py", "services/export/pdf_v1/render.py")]
    br = blast_radius(SPEC, MANIFEST, edges)
    assert br["inbound_count"] == 2
    assert br["by_owner_feature"] == {"editor": 1, "unmapped": 1}
    pr = cleanup_pr_outline(SPEC, br, ["services/export/pdf_v1/api.py", "services/export/pdf_v1/test_api.py",
                                       "services/editor/toolbar.py"])
    assert pr["files_to_delete"] == ["services/export/pdf_v1/api.py", "services/export/pdf_v1/test_api.py"]
    assert pr["files_to_edit"] == ["scripts/nightly.py", "services/editor/toolbar.py"]
    assert "legacy-pdf-export" in pr["body_markdown"]


def test_happy_path_through_every_state():
    p = plan(SunsetState.CANDIDATE)
    with pytest.raises(TransitionError) as e:
        advance(p, {"blast_radius": {"inbound_references": [{"from_path": "x", "acknowledged": False}]}})
    assert e.value.code == "unacknowledged_dependencies"
    r = advance(p, {"blast_radius": {"inbound_references": [{"from_path": "x", "acknowledged": True}]}})
    assert r.to_state is SunsetState.BLAST_RADIUS_CHECKED

    acct = DossierAccount("a", "Initech", 48_000, "pro", date(2027, 1, 5), 4, 0.2, 0.55, "sam@northwind.io")
    dossier = impact_dossier("Legacy PDF Exporter", "PDF Export v2", [acct], date(2027, 1, 31))
    assert "Initech" in dossier["accounts"][0]["email_draft"]
    p = plan(SunsetState.BLAST_RADIUS_CHECKED)
    with pytest.raises(TransitionError) as e:
        advance(p, {"impact_dossier": dossier})
    assert e.value.code == "cs_signoff_required"
    r = advance(p, {"impact_dossier": dossier, "cs_signoff": True})
    assert r.to_state is SunsetState.CUSTOMER_OUTREACH

    p = plan(SunsetState.CUSTOMER_OUTREACH, impact_dossier=dossier)
    with pytest.raises(TransitionError) as e:
        advance(p, {}, now=T0 + timedelta(days=3))
    assert e.value.code == "outreach_pending"
    r = advance(p, {}, now=T0 + timedelta(days=15))
    assert r.to_state is SunsetState.CANARY_DARK_LAUNCH and r.updates["canary_phase"] == "zero_usage_off"

    p = plan(SunsetState.CANARY_DARK_LAUNCH, canary_phase="zero_usage_off", canary=r.updates["canary"])
    r = advance(p, {}, now=T0 + timedelta(days=16))
    assert r.to_state is SunsetState.CANARY_DARK_LAUNCH and r.action == "canary_promote"
    p = plan(SunsetState.CANARY_DARK_LAUNCH, canary_phase="fully_off", canary=r.updates["canary"])
    with pytest.raises(TransitionError) as e:
        advance(p, {"cleanup_pr": {"title": "x"}}, now=T0 + timedelta(days=30))
    assert e.value.code == "scream_test_running"
    with pytest.raises(TransitionError) as e:
        advance(p, {"cleanup_pr": {"title": "x"}, "tickets_since_off": 2}, now=T0 + timedelta(days=50))
    assert e.value.code == "customer_screamed"
    r = advance(p, {"cleanup_pr": {"title": "x"}}, now=T0 + timedelta(days=50))
    assert r.to_state is SunsetState.DEAD_CODE_PR_OPENED

    p = plan(SunsetState.DEAD_CODE_PR_OPENED)
    with pytest.raises(TransitionError):
        advance(p, {"pr_merged": False})
    assert advance(p, {"pr_merged": True}).to_state is SunsetState.ARCHIVED


def test_guards_regulatory_noflag_rollback_cancel():
    with pytest.raises(TransitionError) as e:
        advance(plan(SunsetState.CANDIDATE, feature_is_regulatory=True), {})
    assert e.value.code == "regulatory"
    with pytest.raises(TransitionError) as e:
        advance(plan(SunsetState.CUSTOMER_OUTREACH, flag_key=None), {}, now=T0 + timedelta(days=20))
    assert e.value.code == "no_flag"
    rb = rollback(plan(SunsetState.CANARY_DARK_LAUNCH, canary={"rollbacks": 1}), "Globex escalated")
    assert rb.to_state is SunsetState.CUSTOMER_OUTREACH and rb.updates["canary"]["rollbacks"] == 2
    assert rb.updates["flag_action"] == "re_enable"
    with pytest.raises(TransitionError):
        rollback(plan(SunsetState.CANDIDATE), "x")
    assert cancel(plan(SunsetState.CANARY_DARK_LAUNCH), "x").updates["flag_action"] == "re_enable"
    with pytest.raises(TransitionError):
        cancel(plan(SunsetState.ARCHIVED), "x")


def test_simulator_counts_shared_accounts_once():
    m = trailing_months(date(2026, 10, 1), 3)
    assert m == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    rows = {"a": [FeaturePnl("a", "a", x, eng_hours=60, c_eng=5700, c_support=1200, c_infra=400) for x in m],
            "b": [FeaturePnl("b", "b", x, eng_hours=20, c_eng=1900, c_support=0, c_infra=100) for x in m]}
    exp = [("acct1", "Initech", 48_000, 0.2, "a"), ("acct1", "Initech", 48_000, 0.5, "b"),
           ("acct2", "Hooli", 12_000, 0.1, "a")]
    out = simulate(rows, exp, OrgSettings(), migration_success=0.7)
    assert out["reclaimed_hours_month"] == pytest.approx(72.0)
    assert out["avoidable_cost_month"] == pytest.approx(5700 * 0.9 + 1200 + 400 + 1900 * 0.9 + 100)
    assert out["accounts_affected"] == 2
    assert out["expected_arr_loss"] == pytest.approx(48_000 * 0.5 * 0.3 + 12_000 * 0.1 * 0.3)
    assert out["fte_reclaimed"] == pytest.approx(72 / 112, abs=0.01)
