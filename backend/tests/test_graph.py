"""Phase 3: precedence, the 0.80 threshold, raw-text disposal, structural inference, manifest push sync."""
import asyncio
import json
from pathlib import Path

import pytest

from app.core.manifest import parse_manifest
from app.graph import manifest_store, semantic_classifier as sc
from app.graph.embeddings import HashingEmbedder, cosine
from app.graph.manifest_sync import PayloadFetcher, handle_push, plan_push
from app.graph.precedence import AUTO_CONFIRM, Candidate, EffectiveResolver, resolve
from app.graph.structural_inference import cluster_root, infer
from app.seed.generate import build

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_TEXT = (ROOT / "features.yaml").read_text()
M = parse_manifest(MANIFEST_TEXT)
PII = ["Jane Doe", "jane.doe@example.com", "+1 555 0100", "1 555 0100"]


# ------------------------------------------------------------------------------------- precedence
def test_human_outranks_manifest_outranks_machine():
    c = [Candidate("pdf-export-v2", "ai", 0.99), Candidate("legacy-pdf-export", "manifest"),
         Candidate("document-editor", "human")]
    assert resolve(c).feature_key == "document-editor" and resolve(c).source == "human"
    assert resolve(c[:2]).source == "manifest"
    assert resolve([Candidate("a", "heuristic", 0.85), Candidate("b", "ai", 0.91)]).feature_key == "b"


def test_threshold_is_inclusive_at_080():
    assert AUTO_CONFIRM == 0.80
    assert resolve([Candidate("a", "ai", 0.80)]).action == "map"
    low = resolve([Candidate("a", "ai", 0.7999)])
    assert low.action == "suggest" and low.feature_key == "a"
    assert resolve([]).action == "unmapped"


def test_effective_resolver_paths():
    r = EffectiveResolver(M, human=[("services/export/pdf_v1/render/**", "pdf-export-v2")],
                          machine=[("web/src/features/export-pdf-legacy-print/**", "legacy-pdf-export", 0.83),
                                   ("services/notifications/digest/**", "comments-mentions", 0.79)])
    assert r.owner("services/export/pdf_v1/render/x.py") == ("pdf-export-v2", "human")       # human beats manifest
    assert r.owner("services/export/pdf_v1/api.py") == ("legacy-pdf-export", "manifest")
    assert r.owner("web/src/features/export-pdf-legacy-print/a.tsx") == ("legacy-pdf-export", "heuristic")
    assert r.owner("services/notifications/digest/a.py") == (None, None)                     # 0.79: not mapped
    shares = r.allocate([{"path": "services/notifications/digest/a.py", "additions": 50, "deletions": 0},
                         {"path": "web/src/features/export-pdf-legacy-print/a.tsx", "additions": 50, "deletions": 0}])
    assert shares == {"legacy-pdf-export": (0.5, "heuristic")}          # the other half stays unmapped


# ------------------------------------------------------------------------------------- Tier 2
def test_structural_inference_on_seed_company():
    s = build(MANIFEST_TEXT)
    prs = [[p["path"] for p in w["paths"]] for w in s.extras["wi_rows"] if w["paths"]]
    edges = s.extras["demo_edges"] + [("scripts/maintenance/nightly_reports.py", "services/export/pdf_v1/render.py")]
    props = {p.directory: p for p in infer(EffectiveResolver(M), {x for ps in prs for x in ps}, edges, prs)}
    lp = props["web/src/features/export-pdf-legacy-print"]
    assert lp.feature_key == "legacy-pdf-export" and lp.confidence >= AUTO_CONFIRM
    assert lp.rationale.startswith("Imports 2 modules from legacy-pdf-export")
    for d, key in (("services/notifications/digest", "comments-mentions"),
                   ("services/integrations/zapier_v1_compat", "zapier-v1-webhooks")):
        assert props[d].feature_key == key and props[d].confidence < AUTO_CONFIRM      # triage, not a guess
    assert props["scripts/maintenance"].confidence < 0.5
    assert cluster_root("services/notifications/digest/handlers/x.py") == "services/notifications/digest"


# ------------------------------------------------------------------------------------- Tier 3
class SpyConn:
    """Answers the classifier's queries and records every SQL argument it is sent."""

    def __init__(self, tickets):
        self.tickets, self.args, self.sql = tickets, [], []

    def _log(self, sql, args):
        self.sql.append(sql)
        self.args.append(json.dumps(args, default=str))

    async def fetch(self, sql, *args):
        self._log(sql, args)
        if "FROM mapping_suggestions WHERE decided_at" in sql:
            return []
        if "FROM support_tickets" in sql:
            return [{"id": t, "external_id": f"ZD-{t}"} for t in self.tickets]
        if "SELECT id, key FROM features" in sql:
            return [{"id": f"id-{f.key}", "key": f.key} for f in M.features]
        return []

    async def fetchval(self, sql, *args):
        self._log(sql, args)
        return "to_regclass" in sql

    async def execute(self, sql, *args):
        self._log(sql, args)

    async def executemany(self, sql, rows):
        for r in rows:
            self._log(sql, r)


def _vault():
    v = sc.TextVault()
    v.put("zendesk_ticket", "1", "Hi, this is Jane Doe (jane.doe@example.com, +1 555 0100). Legacy PDF Exporter keeps "
                                  "failing: pdf exported does nothing for our admins")
    v.put("zendesk_ticket", "2", "Hi, this is Jane Doe (jane.doe@example.com). Something stopped working, please call")
    return v


def test_raw_text_never_reaches_the_database_and_is_wiped():
    conn, vault = SpyConn(["1", "2"]), _vault()
    stats = asyncio.run(sc.run_semantic(conn, "org", M, vault, HashingEmbedder(), sc.TemplateRationale()))
    assert stats["classified"] == 2 and stats["auto_confirmed"] == 1 and stats["suggested"] == 1
    assert len(vault) == 0 and "Jane" not in repr(vault)
    blob = "\n".join(conn.args)
    for needle in PII + ["keeps failing", "Something stopped working"]:
        assert needle not in blob, f"raw text leaked into SQL: {needle!r}"
    assert any("INSERT INTO subject_embeddings" in q for q in conn.sql)       # vectors ARE stored
    assert any("UPDATE support_tickets" in q for q in conn.sql)               # confident one auto-mapped


def test_classify_wipes_text_even_when_embedding_fails():
    class Boom:
        model = "hashing-x"

        async def embed(self, texts):
            raise RuntimeError("embedding service down")
    items = [sc.TextItem("support_ticket", "1", "ZD-1", "secret text from Jane Doe")]
    with pytest.raises(RuntimeError):
        asyncio.run(sc.classify(items, M, Boom(), sc.TemplateRationale(), {"x": [1.0]}))
    assert items[0].text is None


@pytest.mark.parametrize("conf, auto", [(0.79, False), (0.80, True)])
def test_semantic_threshold(monkeypatch, conf, auto):
    monkeypatch.setattr(sc, "calibrate", lambda model, s1, s2: conf)
    conn = SpyConn(["1"])
    v = sc.TextVault()
    v.put("zendesk_ticket", "1", "Legacy PDF Exporter export pdf fails")
    stats = asyncio.run(sc.run_semantic(conn, "org", M, v, HashingEmbedder(), sc.TemplateRationale()))
    assert stats["auto_confirmed"] == (1 if auto else 0)
    assert any("UPDATE support_tickets" in q for q in conn.sql) is auto
    suggestion = [a for q, a in zip(conn.sql, conn.args) if "INSERT INTO mapping_suggestions" in q][0]
    assert ('"confirmed"' if auto else '"pending"') in suggestion


def test_rationale_is_redacted_and_template_only_uses_feature_vocabulary():
    assert sc.redact("mail jane.doe@example.com or +1 555 010 0199") == "mail [email] or [number]"
    why = asyncio.run(sc.TemplateRationale().explain("Jane Doe says the pdf export broke", M.by_key()["legacy-pdf-export"], 0.4))
    assert "Jane" not in why and "pdf" in why


def test_hashing_embedder_is_deterministic_and_unit_length():
    e = HashingEmbedder()
    a, b = asyncio.run(e.embed(["PDF export fails", "PDF export fails"]))
    assert a == b and abs(cosine(a, a) - 1) < 1e-9 and len(a) == 1536


# ------------------------------------------------------------------------------------- manifest push
def push(files_changed, ref="refs/heads/main", removed=()):
    return {"ref": ref, "after": "abc12345def", "repository": {"full_name": "acme/app", "default_branch": "main"},
            "commits": [{"added": [], "modified": list(files_changed), "removed": list(removed)}]}


def test_plan_push_filters_branch_and_paths():
    assert plan_push(push(["features.yaml"])).relevant
    assert not plan_push(push(["features.yaml"], ref="refs/heads/feature-x")).relevant
    assert not plan_push(push(["README.md"])).relevant
    p = plan_push(push(["features.yaml", ".featureledger/features.yaml"]))
    assert p.changed[0] == ".featureledger/features.yaml"
    assert plan_push(push([], removed=["features.yaml"])).removed == ["features.yaml"]


class RecConn(SpyConn):
    def __init__(self):
        super().__init__([])
        self.current_sha = None

    async def fetchval(self, sql, *args):
        self._log(sql, args)
        if "SELECT content_sha FROM manifest_versions" in sql:
            return self.current_sha
        return 1


def test_valid_push_is_validated_and_applied(monkeypatch):
    applied = {}

    async def fake_apply(conn, org, manifest):
        applied["features"] = len(manifest.features)
        return {"features": len(manifest.features)}
    monkeypatch.setattr(manifest_store, "apply_manifest", fake_apply)
    conn = RecConn()
    payload = {**push(["features.yaml"]), "featureledger_sandbox_files": {"features.yaml": MANIFEST_TEXT}}
    res = asyncio.run(handle_push(conn, "org", payload, PayloadFetcher(payload)))
    assert res.applied and applied["features"] == len(M.features)
    assert any("'applied'" in q for q in conn.sql)


def test_invalid_push_records_errors_and_changes_nothing(monkeypatch):
    async def must_not_apply(*a, **k):
        raise AssertionError("an invalid manifest must never be applied")
    monkeypatch.setattr(manifest_store, "apply_manifest", must_not_apply)
    conn = RecConn()
    bad = "version: 1\nfeatures:\n  - {key: Bad_Key, name: X}\n  - {key: ok-one, name: Y, tier_gate: platinum}\n"
    payload = {**push(["features.yaml"]), "featureledger_sandbox_files": {"features.yaml": bad}}
    res = asyncio.run(handle_push(conn, "org", payload, PayloadFetcher(payload)))
    r = res.results[0]
    assert r["status"] == "invalid" and len(r["errors"]) == 2 and not res.applied
    assert any("'invalid'" in q for q in conn.sql)
    assert any("UPDATE integrations SET last_error" in q for q in conn.sql)       # surfaced on integration health
    assert not any("asset_feature_mappings" in q or "INSERT INTO features" in q for q in conn.sql)


def test_unchanged_push_is_a_no_op(monkeypatch):
    async def must_not_apply(*a, **k):
        raise AssertionError("unchanged manifest re-applied")
    monkeypatch.setattr(manifest_store, "apply_manifest", must_not_apply)
    conn = RecConn()
    conn.current_sha = manifest_store.sha(MANIFEST_TEXT)
    payload = {**push(["features.yaml"]), "featureledger_sandbox_files": {"features.yaml": MANIFEST_TEXT}}
    assert asyncio.run(handle_push(conn, "org", payload, PayloadFetcher(payload))).results[0]["status"] == "unchanged"
