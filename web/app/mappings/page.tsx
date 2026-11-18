"use client";

import { useCallback, useEffect, useState } from "react";
import { Gated, NEEDS, useCan } from "@/components/Auth";
import { ErrorBox, SectionTitle } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { num } from "@/lib/format";

type Item = {
  id: string; subject_type: "work_item" | "support_ticket" | "code_asset"; subject_label: string; confidence: number;
  source: string; rationale: string; status: string; tier: 2 | 3 | null; auto_confirmed: boolean;
  evidence: Record<string, unknown>; suggested_feature_key: string | null; suggested_feature_name: string | null;
  decided_feature_name: string | null; decided_at: string | null; updated_at: string;
};
type Queue = { items: Item[]; features: { key: string; name: string }[]; threshold: number;
  counts: { pending: number; auto: number; decided: number; tier2: number; tier3: number } };
type Version = { id: number; source: string; repo: string | null; path: string | null; commit_sha: string | null;
  status: string; errors: string[]; summary: Record<string, number>; received_at: string };
type View = "pending" | "auto" | "decided";

const SUBJECT: Record<Item["subject_type"], string> = { support_ticket: "Ticket", work_item: "PR / issue", code_asset: "Code path" };
const TIER: Record<number, { label: string; cls: string }> = {
  2: { label: "Tier 2 · structural", cls: "border-[#c9dcf5] bg-[#eef5fd] text-[#1d5fae]" },
  3: { label: "Tier 3 · semantic", cls: "border-[#d9d2f3] bg-[#f4f1fd] text-[#4b3aa8]" },
};

export default function MappingsPage() {
  const [view, setView] = useState<View>("pending");
  const [tier, setTier] = useState<0 | 2 | 3>(0);
  const [q, setQ] = useState<Queue | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(0);

  const load = useCallback(() => {
    const t = tier ? `&tier=${tier}` : "";
    apiGet<Queue>(`/api/v1/triage?status=${view}${t}`).then(setQ).catch((e) => setErr(e.message));
    apiGet<Version[]>("/api/v1/manifest/versions").then(setVersions).catch(() => undefined);
  }, [view, tier]);
  useEffect(() => { load(); }, [load]);

  const decide = async (i: Item, decision: "confirm" | "reject" | "reassign", feature_key?: string) => {
    setErr(null);
    try {
      const r = await apiPost<{ reallocated_prs: number }>(`/api/v1/triage/${i.id}`, { decision, feature_key });
      setQ((x) => x && { ...x, items: x.items.filter((y) => y.id !== i.id) });
      setDirty((n) => n + 1);
      setNote(`Saved as a human decision${r.reallocated_prs ? ` · ${r.reallocated_prs} PR(s) re-allocated` : ""}.`);
    } catch (e) { setErr((e as Error).message); }
  };
  const runGraph = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await apiPost<{ reallocated: number; tier2: { clusters: number; auto_confirmed: number; suggested: number } }>("/api/v1/graph/run");
      setNote(`Inference ran: ${r.tier2.clusters} unmapped directories scored, ${r.tier2.auto_confirmed} auto-confirmed, ${r.reallocated} PRs re-allocated, P&L recomputed.`);
      setDirty(0); load();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  const recompute = async () => {
    setBusy(true);
    try { await apiPost("/api/v1/jobs/compute-pnl"); setDirty(0); setNote("P&L recomputed with your decisions."); }
    catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };

  const canDecide = useCan()("triage:decide");
  const c = q?.counts;
  const latest = versions[0];

  return (
    <div className="mx-auto max-w-[1180px] space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Feature Graph</h1>
          <p className="max-w-2xl text-sm text-ink-3">
            Which feature owns each file, PR and ticket. features.yaml decides first; Tier 2 (import graph, co-change, naming)
            and Tier 3 (semantic similarity) fill the gaps. Anything below 80% confidence waits here for you and is booked
            as unmapped until then.
          </p>
        </div>
        <div className="flex gap-2">
          <Gated perm="graph:run" disabled={busy} onClick={runGraph}>Run inference</Gated>
          <Gated perm="pnl:recompute" className="btn-primary" disabled={busy} onClick={recompute}>Recompute P&L{dirty ? ` (${dirty})` : ""}</Gated>
        </div>
      </header>

      <div className="card flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-xs text-ink-2">
        <span className="label">Precedence</span>
        {["Your decision", "features.yaml", "Auto ≥ 80%", "Triage < 80% (unmapped meanwhile)"].map((t, n) => (
          <span key={t} className="inline-flex items-center gap-2"><span className="num text-ink-3">{n + 1}</span>{t}{n < 3 && <span className="text-ink-3">›</span>}</span>
        ))}
      </div>

      {latest && (
        <div className={`card flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm ${latest.status === "invalid" ? "border-[#f1c3b0] bg-[#fdf3ee]" : ""}`}>
          <div>
            <span className="font-medium">features.yaml</span>{" "}
            <span className="text-ink-3">· {latest.source.replace("_", " ")}{latest.commit_sha ? ` @ ${latest.commit_sha.slice(0, 8)}` : ""} · {latest.received_at.slice(0, 16).replace("T", " ")}</span>
          </div>
          <span className={`text-xs font-medium ${latest.status === "invalid" ? "text-[#9b1c1c]" : "text-[#17663a]"}`}>
            {latest.status === "invalid" ? `Rejected: ${latest.errors.length} error(s), previous manifest still active` : latest.status}
          </span>
          {latest.status === "invalid" && (
            <ul className="w-full list-disc pl-5 text-xs text-[#8a3413]">{latest.errors.slice(0, 6).map((e) => <li key={e}>{e}</li>)}</ul>
          )}
        </div>
      )}

      {err && <ErrorBox error={err} />}
      {note && <div className="card px-4 py-2.5 text-sm text-ink-2">{note}</div>}

      <div className="flex flex-wrap items-center gap-2">
        {([["pending", "Needs review", c?.pending], ["auto", "Auto-confirmed", c?.auto], ["decided", "Decided", c?.decided]] as const).map(([v, label, n]) => (
          <button key={v} onClick={() => setView(v)}
            className={`rounded-full border px-3 py-1 text-xs ${view === v ? "border-ink bg-ink text-white" : "border-rule bg-white text-ink-2 hover:bg-[#f3f2ee]"}`}>
            {label} <span className="opacity-60">{n ?? "–"}</span>
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-rule" />
        {([[0, "All tiers"], [2, "Structural"], [3, "Semantic"]] as const).map(([t, label]) => (
          <button key={t} onClick={() => setTier(t)}
            className={`rounded-full border px-3 py-1 text-xs ${tier === t ? "border-ink-2 bg-[#efeee9] text-ink" : "border-rule bg-white text-ink-2"}`}>{label}</button>
        ))}
      </div>

      <section className="card divide-y divide-rule">
        {q && q.items.length === 0 && <div className="px-4 py-10 text-center text-sm text-ink-3">Nothing here.</div>}
        {q?.items.slice(0, 150).map((i) => (
          <div key={i.id} className="grid gap-3 px-4 py-3 md:grid-cols-[1fr_220px_auto] md:items-center">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                {i.tier && <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${TIER[i.tier].cls}`}>{TIER[i.tier].label}</span>}
                <span className="text-[11px] text-ink-3">{SUBJECT[i.subject_type]}</span>
                <span className="truncate text-sm font-medium">{i.subject_label}</span>
              </div>
              <div className="mt-1 text-xs text-ink-2">{i.rationale}</div>
              <Evidence item={i} />
            </div>
            <div className="text-sm">
              {view === "decided"
                ? <><span className="text-ink-3">{i.status} →</span> <b>{i.decided_feature_name ?? "none"}</b></>
                : i.suggested_feature_name ? <>→ <b>{i.suggested_feature_name}</b></> : <span className="text-ink-3">no suggestion</span>}
              <Confidence value={i.confidence} threshold={q.threshold} />
            </div>
            {view !== "decided" && !canDecide && (
              <span className="text-xs text-ink-3" title={`Requires ${NEEDS["triage:decide"]}`}>read-only</span>
            )}
            {view !== "decided" && canDecide && (
              <div className="flex items-center gap-2">
                {view === "pending"
                  ? <button className="btn" disabled={!i.suggested_feature_key} onClick={() => decide(i, "confirm")}>Confirm</button>
                  : <button className="btn" onClick={() => decide(i, "confirm")}>Keep</button>}
                <select defaultValue="" onChange={(e) => e.target.value && decide(i, "reassign", e.target.value)}
                  className="rounded-md border border-rule bg-white px-2 py-1.5 text-sm">
                  <option value="">Reassign…</option>
                  {q.features.map((f) => <option key={f.key} value={f.key}>{f.name}</option>)}
                </select>
                <button className="btn" onClick={() => decide(i, "reject")}>{view === "auto" ? "Undo" : "Reject"}</button>
              </div>
            )}
          </div>
        ))}
      </section>

      {versions.length > 1 && (
        <section>
          <SectionTitle title="Manifest history" sub="Every features.yaml seen: GitHub pushes, disk and API" />
          <div className="card divide-y divide-rule text-xs">
            {versions.slice(0, 10).map((v) => (
              <div key={v.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2">
                <span className="text-ink-3">{v.received_at.slice(0, 16).replace("T", " ")} · {v.source.replace("_", " ")}{v.commit_sha ? ` @ ${v.commit_sha.slice(0, 8)}` : ""}</span>
                <span className={v.status === "invalid" ? "font-medium text-[#9b1c1c]" : "text-ink-2"}>
                  {v.status}{v.status === "invalid" ? ` (${v.errors.length} errors)` : v.summary?.features ? ` · ${num(v.summary.features)} features` : ""}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function Confidence({ value, threshold }: { value: number; threshold: number }) {
  const pct = Math.round(value * 100);
  return (
    <div className="mt-1">
      <div className="relative h-1.5 w-full rounded-full bg-[#ecebe7]">
        <div className={`h-1.5 rounded-full ${value >= threshold ? "bg-ink" : "bg-ink-3"}`} style={{ width: `${pct}%` }} />
        <div className="absolute -top-0.5 h-2.5 w-px bg-[#b4471c]" style={{ left: `${threshold * 100}%` }} title="auto-confirm threshold" />
      </div>
      <div className="num text-[11px] text-ink-3">{pct}% confidence{value >= threshold ? " · auto" : ""}</div>
    </div>
  );
}

function Evidence({ item }: { item: Item }) {
  const e = item.evidence as Record<string, number | string | string[] | Record<string, unknown> | undefined>;
  const chips: string[] = [];
  if (item.tier === 2) {
    if (typeof e.s_imports === "number") chips.push(`imports ${Math.round((e.s_imports as number) * 100)}%`);
    if (typeof e.s_cochange === "number") chips.push(`co-change ${Math.round((e.s_cochange as number) * 100)}%`);
    if (typeof e.s_naming === "number") chips.push(`naming ${Math.round((e.s_naming as number) * 100)}%`);
  }
  if (item.tier === 3) {
    if (typeof e.similarity === "number") chips.push(`similarity ${(e.similarity as number).toFixed(2)}`);
    if (e.runner_up) chips.push(`runner-up ${e.runner_up} ${(e.runner_up_similarity as number)?.toFixed?.(2) ?? ""}`);
    if (e.model) chips.push(String(e.model));
  }
  if (!chips.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {chips.map((c) => <span key={c} className="rounded border border-rule bg-white px-1.5 py-0.5 text-[10px] text-ink-3">{c}</span>)}
    </div>
  );
}
