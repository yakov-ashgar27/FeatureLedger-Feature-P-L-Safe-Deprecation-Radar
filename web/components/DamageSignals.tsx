"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";
import type { DamageSignal } from "@/lib/types";

const KIND_LABEL: Record<DamageSignal["kind"], string> = { error: "Error", deprecated_call: "Call to a retired route" };

/** Production errors and calls to a feature's retired API routes. Any signal not dismissed blocks the scream test. */
export function DamageSignals({ featureKey, since, canDismiss, onChange }: {
  featureKey: string; since?: string | null; canDismiss: boolean; onChange?: () => void;
}) {
  const [items, setItems] = useState<DamageSignal[] | null>(null);
  const [reason, setReason] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => {
    apiGet<DamageSignal[]>(`/api/v1/damage-signals?feature_key=${encodeURIComponent(featureKey)}&limit=20`)
      .then(setItems).catch((e) => setErr((e as Error).message));
  }, [featureKey]);
  useEffect(load, [load]);

  const dismiss = async (s: DamageSignal) => {
    setErr(null);
    try {
      await apiPost(`/api/v1/damage-signals/${s.id}/dismiss`, { reason: reason[s.id] || "" });
      load(); onChange?.();
    } catch (e) { setErr((e as Error).message); }
  };

  const shown = (items ?? []).filter((s) => !since || s.occurred_at >= since);
  if (items && !shown.length) return null;
  return (
    <div className="mt-3 border-t border-rule pt-3">
      <div className="label mb-1">Production damage signals</div>
      {err && <div className="text-xs text-[#b4471c]">{err}</div>}
      <ul className="space-y-2 text-xs">
        {shown.map((s) => (
          <li key={s.id}>
            <div><b>{KIND_LABEL[s.kind]}</b> · {s.occurrences.toLocaleString()}× · {s.source} · {s.occurred_at.slice(0, 16).replace("T", " ")}
              <span className="text-ink-3"> · matched by {s.matched_by}</span></div>
            <div className="text-ink-2">{s.title ?? s.route ?? s.external_id}</div>
            {canDismiss && (
              <div className="mt-1 flex gap-2">
                <input className="flex-1 rounded-md border border-rule bg-white px-2 py-1" placeholder="Why is it unrelated?"
                  value={reason[s.id] ?? ""} onChange={(e) => setReason({ ...reason, [s.id]: e.target.value })} />
                <button className="btn text-xs" disabled={(reason[s.id] ?? "").trim().length < 3} onClick={() => dismiss(s)}>Dismiss</button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
