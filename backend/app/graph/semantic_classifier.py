"""Tier 3: semantic classifier over pgvector embeddings.

Privacy (D5): raw text NEVER reaches the database. Connectors hand text to an in-memory `TextVault`
(not to the envelope payload); the classifier embeds it, writes a one-line rationale, and wipes every
`TextItem.text` in a `finally` block before any SQL runs. What is stored: the vector, the confidence, a
non-quoting rationale and a non-content label ("ZD-70012", "NW-10123").

Centroids: per feature, the embedding of its manifest descriptor (name, description, path words, events,
labels, tags), blended 50/50 with the mean embedding of subjects a HUMAN confirmed for it, so triage
decisions teach the classifier. Machine-confirmed subjects never feed the centroid (no self-reinforcement).

Confidence = calibrated similarity to the best centroid, discounted by a small margin over the runner-up.
>= 0.80 maps automatically (never over a manifest or human mapping); below goes to triage.
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..core.manifest import FeatureSpec, Manifest
from .embeddings import Embedder, cosine, default_embedder, to_pgvector, words
from .precedence import AUTO_CONFIRM

log = logging.getLogger("featureledger.tier3")
CALIBRATION = {  # (similarity floor, similarity span, margin span) per embedding family
    "hashing": (0.06, 0.25, 0.16),
    "default": (0.20, 0.35, 0.10),
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
NUM_RE = re.compile(r"\+?\d[\d\s().-]{5,}\d")


class TextVault:
    """In-memory only. Never logged, never serialized, cleared at the end of every job."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], str] = {}

    def put(self, entity: str, external_id: str, text: str) -> None:
        if text and text.strip():
            self._d[(entity, str(external_id))] = text

    def get(self, entity: str, external_id: str) -> str | None:
        return self._d.get((entity, str(external_id)))

    def keys(self, entity: str) -> list[str]:
        return [k for e, k in self._d if e == entity]

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)

    def __repr__(self) -> str:  # never print contents
        return f"<TextVault {len(self._d)} items>"


@dataclass
class TextItem:
    subject_type: str          # support_ticket | work_item
    subject_id: str            # row id
    label: str                 # content-free label for the triage queue
    text: str | None           # RAW text: memory only, wiped by classify()
    apply: dict[str, Any] = field(default_factory=dict)   # how to apply a confident mapping


@dataclass
class Classification:
    item: TextItem
    feature_key: str
    confidence: float
    similarity: float
    runner_up: str | None
    runner_up_similarity: float
    rationale: str
    vector: list[float]


def descriptor(f: FeatureSpec) -> str:
    lit = " ".join(g.split("*")[0].replace("/", " ").replace("_", " ").replace("-", " ") for g in f.paths)
    ev = " ".join(e.replace("_", " ") for e in f.events + f.event_aliases)
    return " ".join([f.name, f.name, f.description or "", f.key.replace("-", " "), lit, ev,
                     " ".join(f.jira_labels), " ".join(t.replace("-", " ") for t in f.support_tags)])


def calibrate(model: str, s1: float, s2: float) -> float:
    floor, span, mspan = CALIBRATION["hashing" if model.startswith("hashing") else "default"]
    sim = min(max((s1 - floor) / span, 0.0), 1.0)
    margin = min(max((s1 - s2) / mspan, 0.0), 1.0)
    # decided at the stored precision (numeric(3,2)): what triage shows is exactly what was compared
    return round(min(0.98, 0.6 * sim + 0.4 * margin), 2)


def redact(s: str) -> str:
    s = EMAIL_RE.sub("[email]", s)
    s = NUM_RE.sub("[number]", s)
    return " ".join(s.split())[:160]


class TemplateRationale:
    """No LLM: name the feature vocabulary the text shares (only words from OUR feature descriptor leak)."""

    async def explain(self, text: str, f: FeatureSpec, similarity: float) -> str:
        shared = sorted(set(words(text)) & set(words(descriptor(f))))[:4]
        why = f"mentions {', '.join(shared)}" if shared else "closest in meaning"
        return f"Semantic match to {f.name}: {why} (similarity {similarity:.2f})"


class LLMRationale:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model
        self.fallback = TemplateRationale()

    async def explain(self, text: str, f: FeatureSpec, similarity: float) -> str:
        import httpx
        prompt = [{"role": "system", "content": "In at most 15 words, say why the text below concerns the named product "
                   "feature. Describe the topic only. Never include names, emails, numbers, or quotes from the text."},
                  {"role": "user", "content": f"Feature: {f.name}. {f.description or ''}\nText: {text[:1500]}"}]
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"},
                                 json={"model": self.model, "messages": prompt, "max_tokens": 40, "temperature": 0})
                r.raise_for_status()
                return redact(r.json()["choices"][0]["message"]["content"].strip().strip('"'))
        except Exception:  # noqa: BLE001 - never block classification on the explainer
            return await self.fallback.explain(text, f, similarity)


def default_explainer(env: dict[str, str] | None = None):
    env = os.environ if env is None else env
    if env.get("LLM_API_KEY"):
        return LLMRationale(env["LLM_API_KEY"], env.get("LLM_BASE_URL", "https://api.openai.com/v1"),
                            env.get("LLM_MODEL", "gpt-4o-mini"))
    return TemplateRationale()


async def centroids(manifest: Manifest, embedder: Embedder,
                    human_examples: dict[str, list[list[float]]] | None = None) -> dict[str, list[float]]:
    feats = [f for f in manifest.features if not f.shared_overhead]
    base = await embedder.embed([descriptor(f) for f in feats])
    out = {}
    for f, v in zip(feats, base):
        ex = (human_examples or {}).get(f.key) or []
        if ex:
            mean = [sum(col) / len(ex) for col in zip(*ex)]
            v = [0.5 * a + 0.5 * b for a, b in zip(v, mean)]
            n = sum(x * x for x in v) ** 0.5
            v = [x / n for x in v] if n else v
        out[f.key] = v
    return out


async def classify(items: list[TextItem], manifest: Manifest, embedder: Embedder, explainer,
                   cents: dict[str, list[float]]) -> list[Classification]:
    """Embed, score, explain, then WIPE the raw text of every item (even if something fails)."""
    specs = manifest.by_key()
    try:
        vectors = await embedder.embed([i.text or "" for i in items])
        out = []
        for item, v in zip(items, vectors):
            sims = sorted(((cosine(v, c), k) for k, c in cents.items()), reverse=True)
            if not sims:
                continue
            (s1, k1), (s2, k2) = sims[0], (sims[1] if len(sims) > 1 else (0.0, None))
            conf = calibrate(embedder.model, s1, s2)
            why = await explainer.explain(item.text or "", specs[k1], s1)
            out.append(Classification(item, k1, conf, round(s1, 4), k2, round(s2, 4), redact(why), v))
        return out
    finally:
        for i in items:
            i.text = None


# ------------------------------------------------------------------------------------- database side
async def has_pgvector(conn) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('subject_embeddings') IS NOT NULL"))


async def _human_examples(conn, model: str) -> dict[str, list[list[float]]]:
    rows = await conn.fetch("""
        SELECT f.key, e.embedding::text AS v FROM subject_embeddings e
          JOIN support_tickets t ON e.subject_type = 'support_ticket' AND t.id = e.subject_id
          JOIN features f ON f.id = t.feature_id
         WHERE t.mapping_source = 'human' AND e.model = $1""", model)
    out: dict[str, list[list[float]]] = defaultdict(list)
    for r in rows:
        out[r["key"]].append([float(x) for x in r["v"].strip("[]").split(",")])
    return out


async def collect_items(conn, vault: TextVault) -> list[TextItem]:
    """Unmapped subjects whose text is in this job's vault. Subjects a human already decided are skipped."""
    items: list[TextItem] = []
    decided = {(r["subject_type"], str(r["subject_id"])) for r in await conn.fetch(
        "SELECT subject_type::text, subject_id FROM mapping_suggestions WHERE decided_at IS NOT NULL")}
    zd = vault.keys("zendesk_ticket")
    if zd:
        for r in await conn.fetch("""SELECT id, external_id FROM support_tickets
                                      WHERE feature_id IS NULL AND external_id = ANY($1)""", [f"ZD-{k}" for k in zd]):
            if ("support_ticket", str(r["id"])) not in decided:
                items.append(TextItem("support_ticket", str(r["id"]), r["external_id"],
                                      vault.get("zendesk_ticket", r["external_id"][3:]),
                                      {"kind": "ticket", "id": r["id"]}))
    prs = vault.keys("github_pr")
    if prs:
        for r in await conn.fetch("""SELECT w.id, w.external_id FROM work_items w
                                      WHERE w.item_type = 'pr' AND NOT w.is_sweeping AND w.external_id = ANY($1)
                                        AND NOT EXISTS (SELECT 1 FROM work_item_allocations a WHERE a.work_item_id = w.id)""",
                                   prs):
            if ("work_item", str(r["id"])) not in decided:
                items.append(TextItem("work_item", str(r["id"]), r["external_id"], vault.get("github_pr", r["external_id"]),
                                      {"kind": "pr", "id": r["id"]}))
    issues = vault.keys("jira_issue")
    if issues:
        for r in await conn.fetch("""
            SELECT i.id, i.external_id FROM work_items i
             WHERE i.source = 'jira' AND i.item_type = 'issue' AND i.external_id = ANY($1)
               AND EXISTS (SELECT 1 FROM work_items pr WHERE pr.item_type = 'pr' AND i.external_id = ANY(pr.linked_keys)
                             AND NOT pr.is_sweeping
                             AND NOT EXISTS (SELECT 1 FROM work_item_allocations a WHERE a.work_item_id = pr.id))""",
                issues):
            if ("work_item", str(r["id"])) not in decided:
                items.append(TextItem("work_item", str(r["id"]), r["external_id"], vault.get("jira_issue", r["external_id"]),
                                      {"kind": "jira", "key": r["external_id"]}))
    return items


async def run_semantic(conn, org_id: str, manifest: Manifest, vault: TextVault | None,
                       embedder: Embedder | None = None, explainer=None) -> dict[str, Any]:
    stats = {"classified": 0, "auto_confirmed": 0, "suggested": 0, "embeddings_stored": 0}
    if vault is None or not len(vault):
        return stats
    embedder = embedder or default_embedder()
    explainer = explainer or default_explainer()
    t0 = time.monotonic()
    try:
        items = await collect_items(conn, vault)
    finally:
        vault.clear()   # texts not needed by any item are dropped here, before anything else happens
    log.info("tier3: collected %d unmapped subjects in %.2fs", len(items), time.monotonic() - t0)
    if not items:
        return stats
    pg = await has_pgvector(conn)
    cents = await centroids(manifest, embedder, await _human_examples(conn, embedder.model) if pg else None)
    t0 = time.monotonic()
    results = await classify(items, manifest, embedder, explainer, cents)   # wipes every item.text
    log.info("tier3: embedded and classified %d in %.2fs (%s)", len(results), time.monotonic() - t0, embedder.model)
    fids = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM features")}
    t0 = time.monotonic()
    if pg:
        await conn.executemany("""
            INSERT INTO feature_embeddings (feature_id, org_id, model, embedding) VALUES ($1, $2, $3, $4::vector)
            ON CONFLICT (feature_id) DO UPDATE SET model = EXCLUDED.model, embedding = EXCLUDED.embedding,
                updated_at = now()""",
            [(fids[k], org_id, embedder.model, to_pgvector(v)) for k, v in cents.items() if k in fids])
        await conn.executemany("""
            INSERT INTO subject_embeddings (org_id, subject_type, subject_id, model, embedding)
            VALUES ($1, $2::triage_subject, $3, $4, $5::vector)
            ON CONFLICT (org_id, subject_type, subject_id) DO UPDATE SET model = EXCLUDED.model,
                embedding = EXCLUDED.embedding, created_at = now()""",
            [(org_id, c.item.subject_type, c.item.subject_id, embedder.model, to_pgvector(c.vector)) for c in results])
        stats["embeddings_stored"] = len(results)
    log.info("tier3: stored %d embeddings in %.2fs", stats["embeddings_stored"], time.monotonic() - t0)
    t0 = time.monotonic()
    for c in results:
        stats["classified"] += 1
        fid = fids.get(c.feature_key)
        auto = c.confidence >= AUTO_CONFIRM and fid is not None
        if auto:
            await _apply(conn, org_id, c, fid)
            stats["auto_confirmed"] += 1
        else:
            stats["suggested"] += 1
        await conn.execute("""
            INSERT INTO mapping_suggestions (org_id, subject_type, subject_id, subject_label, suggested_feature_id,
                                             confidence, source, rationale, status, tier, evidence, auto_confirmed,
                                             decided_feature_id)
            VALUES ($1, $2::triage_subject, $3, $4, $5, $6, 'ai', $7, $8::mapping_status, 3, $9, $10, $11)
            ON CONFLICT (org_id, subject_type, subject_id) DO UPDATE SET
                suggested_feature_id = EXCLUDED.suggested_feature_id, confidence = EXCLUDED.confidence,
                source = 'ai', rationale = EXCLUDED.rationale, status = EXCLUDED.status, tier = 3,
                evidence = EXCLUDED.evidence, auto_confirmed = EXCLUDED.auto_confirmed,
                decided_feature_id = EXCLUDED.decided_feature_id, updated_at = now()
             WHERE mapping_suggestions.decided_at IS NULL""",
            org_id, c.item.subject_type, c.item.subject_id, c.item.label, fid, c.confidence, c.rationale,
            "confirmed" if auto else "pending",
            {"similarity": c.similarity, "runner_up": c.runner_up, "runner_up_similarity": c.runner_up_similarity,
             "model": embedder.model, "threshold": AUTO_CONFIRM},
            auto, fid if auto else None)
    log.info("tier3: applied %d decisions in %.2fs", len(results), time.monotonic() - t0)
    return stats


async def _apply(conn, org_id: str, c: Classification, fid) -> None:
    a = c.item.apply
    if a["kind"] == "ticket":   # never over a manifest or human mapping
        await conn.execute("""UPDATE support_tickets SET feature_id = $2, mapping_source = 'ai', mapping_confidence = $3
                               WHERE id = $1 AND (feature_id IS NULL OR mapping_source = 'ai')""", a["id"], fid,
                           c.confidence)
    elif a["kind"] == "pr":
        await conn.execute("""INSERT INTO work_item_allocations (work_item_id, feature_id, org_id, share, source)
                              SELECT $1, $2, $3, 1, 'ai' WHERE NOT EXISTS
                                  (SELECT 1 FROM work_item_allocations WHERE work_item_id = $1)""", a["id"], fid, org_id)
    else:  # jira: every linked PR that nothing else maps
        await conn.execute("""
            INSERT INTO work_item_allocations (work_item_id, feature_id, org_id, share, source)
            SELECT pr.id, $2, $3, 1, 'ai' FROM work_items pr
             WHERE pr.item_type = 'pr' AND $1 = ANY(pr.linked_keys) AND NOT pr.is_sweeping
               AND NOT EXISTS (SELECT 1 FROM work_item_allocations x WHERE x.work_item_id = pr.id)""",
            a["key"], fid, org_id)
