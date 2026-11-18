"use client";

import { useEffect, useMemo, useState } from "react";
import { ClassBadge, ErrorBox, Kpi, SectionTitle } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { num, pct, usd, usdShort } from "@/lib/format";
import type { Ledger, LedgerRow, SimulationResult } from "@/lib/types";

export default function SimulatorPage() {
  const [rows, setRows] = useState<LedgerRow[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [success, setSuccess] = useState(0.7);
  const [window, setWindow] = useState(3);
  const [migrationCost, setMigrationCost] = useState(0);
  const [res, setRes] = useState<SimulationResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    apiGet<Ledger>("/api/v1/pnl?window=3").then((d) => {
      const candidates = d.features.filter((r) => !r.is_shared_overhead && !r.is_regulatory);
      setRows(candidates);
      setPicked(new Set(candidates.filter((r) => r.classification === "zombie").map((r) => r.key)));
    }).catch((e) => setErr(e.message));
  }, []);

  useEffect(() => {
    if (!picked.size) { setRes(null); return; }
    const t = setTimeout(() => {
      apiPost<SimulationResult>("/api/v1/simulate", {
        feature_keys: [...picked], window, migration_success: success, one_off_migration_cost: migrationCost,
      }).then((r) => { setRes(r); setErr(null); }).catch((e) => setErr(e.message));
    }, 200);
    return () => clearTimeout(t);
  }, [picked, success, window, migrationCost]);

  const sorted = useMemo(() => [...rows].sort((a, b) => a.net_roi - b.net_roi), [rows]);
  const toggle = (k: string) => setPicked((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });

  return (
    <div className="mx-auto max-w-[1280px] space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">What-if sunset simulator</h1>
        <p className="text-sm text-ink-3">
          Pick features to retire. Savings count only avoidable cost: 10% of engineering is held back for the migration
          tail, and shared platform overhead is never counted as reclaimed.
        </p>
      </header>
      {err && <ErrorBox error={err} />}
      <div className="grid gap-6 lg:grid-cols-5">
        <section className="lg:col-span-2">
          <SectionTitle title="Features" sub="Zombies are pre-selected. Sorted by net monthly result." />
          <div className="card max-h-[640px] divide-y divide-rule overflow-y-auto">
            {sorted.map((r) => (
              <label key={r.key} className="flex cursor-pointer items-center gap-3 px-3 py-2.5 hover:bg-[#faf9f7]">
                <input type="checkbox" checked={picked.has(r.key)} onChange={() => toggle(r.key)} className="h-4 w-4 accent-black" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{r.name}</div>
                  <div className="num text-xs text-ink-3">net {usdShort(r.net_roi)}/mo · adoption {pct(r.adoption)}</div>
                </div>
                <ClassBadge value={r.classification} />
              </label>
            ))}
          </div>
        </section>
        <section className="space-y-5 lg:col-span-3">
          <div className="card grid gap-5 p-4 sm:grid-cols-3">
            <Slider label="Customers who migrate successfully" value={success} min={0} max={1} step={0.05}
              fmt={pct} onChange={setSuccess} />
            <Slider label="Averaging window (months)" value={window} min={1} max={6} step={1}
              fmt={(v) => String(v)} onChange={setWindow} />
            <Slider label="One-off migration cost" value={migrationCost} min={0} max={200000} step={5000}
              fmt={usdShort} onChange={setMigrationCost} />
          </div>
          {res ? (
            <>
              <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
                <Kpi label="Engineering reclaimed" value={`${num(res.reclaimed_hours_month)} h/mo`} sub={`≈ ${res.fte_reclaimed} FTE`} />
                <Kpi label="Avoidable cost" value={usdShort(res.avoidable_cost_year)} sub={`${usdShort(res.avoidable_cost_month)} per month`} />
                <Kpi label="Expected ARR loss" value={usdShort(res.expected_arr_loss)} tone={res.expected_arr_loss > 0 ? "bad" : undefined}
                  sub={`${res.accounts_affected} accounts · ${usdShort(res.arr_touched)} ARR touched`} />
                <Kpi label="Net annual impact" value={usdShort(res.net_annual_impact)} tone={res.net_annual_impact < 0 ? "bad" : undefined}
                  sub="savings − expected loss − one-off" />
              </div>
              <div className="card overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="border-b border-rule bg-[#f9f8f6]"><tr>
                    <th className="px-3 py-2">Feature</th><th className="px-3 py-2 text-right">Hours / mo</th>
                    <th className="px-3 py-2 text-right">Avoidable / mo</th><th className="px-3 py-2 text-right">Value lost / mo</th></tr></thead>
                  <tbody>
                    {res.features.map((f) => (
                      <tr key={f.feature_key} className="border-b border-rule last:border-0">
                        <td className="px-3 py-2">{rows.find((r) => r.key === f.feature_key)?.name ?? f.feature_key}</td>
                        <td className="num px-3 py-2 text-right">{num(f.reclaimed_hours_month, 1)}</td>
                        <td className="num px-3 py-2 text-right">{usd(f.avoidable_cost_month)}</td>
                        <td className="num px-3 py-2 text-right">{usd(f.attributed_mrr)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div>
                <SectionTitle title="Affected accounts" sub="Each account is counted once, at its highest churn risk across the selected features" />
                <div className="card max-h-80 overflow-y-auto">
                  <table className="w-full text-sm">
                    <tbody>
                      {res.accounts.map((a) => (
                        <tr key={a.account_id} className="border-b border-rule last:border-0">
                          <td className="px-3 py-2 font-medium">{a.name}</td>
                          <td className="px-3 py-2 text-xs text-ink-3">{a.features.join(", ")}</td>
                          <td className="num px-3 py-2 text-right">{usd(a.arr_usd)}</td>
                          <td className={`num px-3 py-2 text-right ${a.churn_risk >= 0.5 ? "font-semibold text-[#b4471c]" : "text-ink-2"}`}>
                            risk {pct(a.churn_risk, 0)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          ) : (
            <div className="card px-4 py-10 text-center text-sm text-ink-3">Select at least one feature.</div>
          )}
        </section>
      </div>
    </div>
  );
}

function Slider({ label, value, min, max, step, fmt, onChange }: {
  label: string; value: number; min: number; max: number; step: number; fmt: (v: number) => string; onChange: (v: number) => void;
}) {
  return (
    <label className="block">
      <div className="flex justify-between text-xs"><span className="text-ink-3">{label}</span>
        <span className="num font-medium">{fmt(value)}</span></div>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))}
        className="mt-2 w-full accent-black" />
    </label>
  );
}
