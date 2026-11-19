"use client";

import Link from "next/link";
import { ConfidenceBadge } from "@/components/Confidence";
import { MarginBleedBadge } from "@/components/TokenBleed";
import { useMemo, useState } from "react";
import { CLASS_META, pct, STATE_LABEL, usd } from "@/lib/format";
import type { FeatureClass, LedgerRow } from "@/lib/types";
import { ClassBadge } from "./ui";

type Col = { key: keyof LedgerRow; label: string; fmt: (r: LedgerRow) => React.ReactNode; num?: boolean };

const COLS: Col[] = [
  { key: "c_eng", label: "Eng", fmt: (r) => usd(r.c_eng), num: true },
  { key: "c_support", label: "Support", fmt: (r) => usd(r.c_support), num: true },
  { key: "c_infra", label: "Cloud infra", fmt: (r) => usd(r.c_infra), num: true },
  { key: "c_ai_tokens", label: "AI tokens", fmt: (r) => usd(r.c_ai_tokens), num: true },
  { key: "c_total", label: "Total cost", fmt: (r) => usd(r.c_total), num: true },
  { key: "value", label: "Value", fmt: (r) => usd(r.value), num: true },
  { key: "net_roi", label: "Net / mo", num: true,
    fmt: (r) => <span className={r.net_roi < 0 ? "text-[#b4471c]" : ""}>{usd(r.net_roi)}</span> },
  { key: "efficiency", label: "Eff.", fmt: (r) => `${r.efficiency.toFixed(2)}×`, num: true },
  { key: "adoption", label: "Adoption", fmt: (r) => pct(r.adoption), num: true },
  { key: "bug_tax_ratio", label: "Bug tax", fmt: (r) => pct(r.bug_tax_ratio, 0), num: true },
  { key: "revenue_at_risk_arr", label: "ARR at risk", fmt: (r) => usd(r.revenue_at_risk_arr), num: true },
];

const FILTERS: (FeatureClass | "all")[] = ["all", "zombie", "niche_anchor", "cash_cow", "growth_bet", "watch", "mandated", "platform"];

export function LedgerTable({ rows }: { rows: LedgerRow[] }) {
  const [sort, setSort] = useState<{ key: keyof LedgerRow; dir: 1 | -1 }>({ key: "net_roi", dir: 1 });
  const [filter, setFilter] = useState<FeatureClass | "all">("all");
  const [q, setQ] = useState("");

  const view = useMemo(() => {
    const f = rows.filter((r) => (filter === "all" || r.classification === filter)
      && (!q || `${r.name} ${r.key} ${r.team}`.toLowerCase().includes(q.toLowerCase())));
    return [...f].sort((a, b) => {
      const av = a[sort.key] as number | string, bv = b[sort.key] as number | string;
      return (av > bv ? 1 : av < bv ? -1 : 0) * sort.dir;
    });
  }, [rows, sort, filter, q]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: rows.length };
    rows.forEach((r) => { c[r.classification] = (c[r.classification] ?? 0) + 1; });
    return c;
  }, [rows]);

  const head = (key: keyof LedgerRow, label: string, right = false) => (
    <th className={`cursor-pointer select-none px-3 py-2.5 ${right ? "text-right" : ""}`}
      onClick={() => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : -1 }))}>
      {label}{sort.key === key ? (sort.dir === 1 ? " ↑" : " ↓") : ""}
    </th>
  );

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-rule px-4 py-3">
        {FILTERS.filter((f) => counts[f]).map((f) => (
          <button key={f} onClick={() => setFilter(f)}
            className={`rounded-full border px-2.5 py-1 text-xs ${filter === f ? "border-ink bg-ink text-white" : "border-rule bg-white text-ink-2 hover:bg-[#f3f2ee]"}`}>
            {f === "all" ? "All" : CLASS_META[f].label} <span className="opacity-60">{counts[f]}</span>
          </button>
        ))}
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search features or teams"
          className="ml-auto w-56 rounded-md border border-rule bg-white px-2.5 py-1.5 text-sm outline-none focus:border-ink-3" />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-rule bg-[#f9f8f6]">
            <tr>
              {head("name", "Feature")}
              {head("classification", "Class")}
              {COLS.map((c) => head(c.key, c.label, true))}
            </tr>
          </thead>
          <tbody>
            {view.map((r) => (
              <tr key={r.key} className="border-b border-rule last:border-0 hover:bg-[#faf9f7]">
                <td className="px-3 py-2.5">
                  <Link href={`/features/${r.key}`} className="font-medium hover:underline">{r.name}</Link>
                  <div className="text-xs text-ink-3">
                    {r.team}{r.tier_gate ? ` · ${r.tier_gate}` : ""}
                    {r.sunset_state ? <span className="ml-1 text-[#b4471c]">· {STATE_LABEL[r.sunset_state]}</span> : null}
                    {r.archived ? <span className="ml-1 rounded bg-[#efeee9] px-1.5 py-px text-[10px] uppercase tracking-wide text-ink-3">archived</span> : null}
                  </div>
                </td>
                <td className="px-3 py-2.5" title={r.classification_reason}>
                  <div className="flex flex-wrap items-center gap-1.5"><ClassBadge value={r.classification} /><ConfidenceBadge c={r.confidence} />{r.margin_bleed && <MarginBleedBadge reason={r.margin_bleed_reason} />}</div>
                </td>
                {COLS.map((c) => <td key={c.key} className="num whitespace-nowrap px-3 py-2.5 text-right">{c.fmt(r)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
