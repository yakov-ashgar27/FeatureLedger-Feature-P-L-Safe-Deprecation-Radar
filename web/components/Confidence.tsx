"use client";

import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api";
import { pct, usd } from "@/lib/format";
import type { Confidence, Explain } from "@/lib/types";

const TONE: Record<Confidence["level"], string> = {
  high: "bg-[#e7f5ee] text-[#126b4a]",
  medium: "bg-[#fff4d6] text-[#7a5300]",
  low: "bg-[#fdf3ee] text-[#8a3413]",
};
const LABEL: Record<Confidence["level"], string> = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };

/** How much of this row rests on measured data. The reasons are in the tooltip. */
export function ConfidenceBadge({ c }: { c?: Pick<Confidence, "level" | "score" | "reasons"> | null }) {
  if (!c) return null;
  return (
    <span title={`${LABEL[c.level]} (${Math.round(c.score * 100)}%)\n• ${c.reasons.join("\n• ")}`} data-confidence={c.level}
      className={`inline-block rounded-full px-1.5 py-px text-[10px] font-medium ${TONE[c.level]}`}>
      {c.level === "high" ? "●●●" : c.level === "medium" ? "●●○" : "●○○"}
    </span>
  );
}

/** "Why this number": every cost and value line, how it is computed, and how much of it is measured. */
export function WhyThisNumber({ featureKey }: { featureKey: string }) {
  const [d, setD] = useState<Explain | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    apiGet<Explain>(`/api/v1/features/${encodeURIComponent(featureKey)}/explain`).then(setD).catch((e) => setErr(e.message));
  }, [featureKey]);
  if (err) return <div className="text-sm text-ink-3">{err}</div>;
  if (!d) return <div className="text-sm text-ink-3">Loading…</div>;
  const c = d.confidence;
  return (
    <div className="space-y-4" data-testid="why-this-number">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${TONE[c.level]}`}>{LABEL[c.level]}</span>
        <span className="text-xs text-ink-3">{d.period} · {Math.round(c.cost_quality * 100)}% of the cost rests on declared or measured data
          {c.has_value_data ? (c.verdict_stable ? " · the verdict holds under both revenue models" : " · the verdict flips between revenue models") : " · no revenue data yet"}</span>
      </div>
      <ul className="space-y-1 text-sm text-ink-2">{c.reasons.map((r) => <li key={r}>• {r}</li>)}</ul>
      <table className="w-full text-sm">
        <thead><tr className="border-b border-rule text-left text-xs text-ink-3"><th className="py-1.5">Line</th><th className="text-right">Amount / mo</th><th className="pl-4">How it is computed</th><th className="text-right">Measured</th></tr></thead>
        <tbody>{d.lines.map((l) => (
          <tr key={l.line} className="border-b border-rule align-top">
            <td className="py-2 pr-3 font-medium">{l.line}</td>
            <td className="num py-2 text-right">{usd(l.amount)}</td>
            <td className="py-2 pl-4 text-xs text-ink-2">{l.how}</td>
            <td className="num py-2 text-right text-xs" title={l.measured_label ?? ""}>{l.measured == null ? "–" : pct(l.measured, 0)}</td>
          </tr>))}
        </tbody>
      </table>
      <p className="text-[11px] text-ink-3">
        Net by model: usage-weighted {usd(c.net_by_model.usage_weighted)}, tier-gated {usd(c.net_by_model.tier_gated)}.
        A feature P&amp;L is an allocation model, not accounting: use it to rank and question features, then confirm with the team.
      </p>
    </div>
  );
}
