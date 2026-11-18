"use client";

import { useCallback, useEffect, useState } from "react";
import { ErrorBox } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import type { TriageItem } from "@/lib/types";

const SUBJECT: Record<TriageItem["subject_type"], string> = {
  support_ticket: "Ticket", work_item: "Pull request", code_asset: "Code path",
};

export default function MappingsPage() {
  const [items, setItems] = useState<TriageItem[]>([]);
  const [features, setFeatures] = useState<{ key: string; name: string }[]>([]);
  const [filter, setFilter] = useState<TriageItem["subject_type"] | "all">("all");
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(0);

  const load = useCallback(() => apiGet<{ items: TriageItem[]; features: { key: string; name: string }[] }>("/api/v1/triage")
    .then((d) => { setItems(d.items); setFeatures(d.features); }).catch((e) => setErr(e.message)), []);
  useEffect(() => { load(); }, [load]);

  const decide = async (item: TriageItem, decision: "confirm" | "reject" | "reassign", feature_key?: string) => {
    try {
      await apiPost(`/api/v1/triage/${item.id}`, { decision, feature_key });
      setItems((xs) => xs.filter((x) => x.id !== item.id));
      setDone((n) => n + 1);
    } catch (e) { setErr((e as Error).message); }
  };

  const shown = items.filter((i) => filter === "all" || i.subject_type === filter);

  return (
    <div className="mx-auto max-w-[1100px] space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Feature Graph triage</h1>
          <p className="text-sm text-ink-3">
            Suggestions below 80% confidence from structural inference (Tier 2) and the semantic classifier (Tier 3).
            Each click becomes a human-confirmed mapping and is used from the next P&L run.
          </p>
        </div>
        <div className="flex items-center gap-3 text-sm">
          {done > 0 && (
            <button className="btn" onClick={async () => { await apiPost("/api/v1/jobs/compute-pnl"); setDone(0); }}>
              Recompute P&L ({done} decided)
            </button>
          )}
          <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)} className="rounded-md border border-rule bg-white px-2 py-1.5 text-sm">
            <option value="all">All ({items.length})</option>
            {(Object.keys(SUBJECT) as TriageItem["subject_type"][]).map((k) => (
              <option key={k} value={k}>{SUBJECT[k]} ({items.filter((i) => i.subject_type === k).length})</option>
            ))}
          </select>
        </div>
      </header>
      {err && <ErrorBox error={err} />}
      <div className="card divide-y divide-rule">
        {shown.length === 0 && <div className="px-4 py-10 text-center text-sm text-ink-3">Queue is empty.</div>}
        {shown.slice(0, 100).map((i) => (
          <div key={i.id} className="flex flex-wrap items-center gap-4 px-4 py-3">
            <div className="w-24 shrink-0 text-xs text-ink-3">{SUBJECT[i.subject_type]}<br />{i.source}</div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{i.subject_label}</div>
              <div className="truncate text-xs text-ink-3">{i.rationale}</div>
            </div>
            <div className="w-48 text-sm">
              {i.suggested_feature_name ? <>→ <span className="font-medium">{i.suggested_feature_name}</span></> : <span className="text-ink-3">no suggestion</span>}
              <div className="mt-1 h-1.5 w-full rounded-full bg-[#ecebe7]">
                <div className="h-1.5 rounded-full bg-ink-2" style={{ width: `${Math.round(i.confidence * 100)}%` }} />
              </div>
              <div className="num text-[11px] text-ink-3">{Math.round(i.confidence * 100)}% confidence</div>
            </div>
            <div className="flex items-center gap-2">
              <button className="btn" disabled={!i.suggested_feature_key} onClick={() => decide(i, "confirm")}>Confirm</button>
              <select defaultValue="" onChange={(e) => e.target.value && decide(i, "reassign", e.target.value)}
                className="rounded-md border border-rule bg-white px-2 py-1.5 text-sm">
                <option value="">Reassign…</option>
                {features.map((f) => <option key={f.key} value={f.key}>{f.name}</option>)}
              </select>
              <button className="btn" onClick={() => decide(i, "reject")}>Reject</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
