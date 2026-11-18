import { Suspense } from "react";
import { Segmented } from "@/components/Controls";
import { LedgerTable } from "@/components/LedgerTable";
import { QuadrantScatter } from "@/components/QuadrantScatter";
import { ErrorBox, Kpi, SectionTitle } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { num, usdShort } from "@/lib/format";
import type { Ledger } from "@/lib/types";

export const dynamic = "force-dynamic";

type SP = { model?: string; view?: string; window?: string; period?: string };

export default async function LedgerPage({ searchParams }: { searchParams: Promise<SP> }) {
  const sp = await searchParams;
  const model = sp.model ?? "usage_weighted";
  const view = sp.view ?? "contribution";
  const win = sp.window ?? "3";
  const qs = new URLSearchParams({ model, view, window: win, ...(sp.period ? { period: sp.period } : {}) });

  let data: Ledger;
  try {
    data = await apiGet<Ledger>(`/api/v1/pnl?${qs}`);
  } catch (e) {
    return <ErrorBox error={`Could not load the ledger: ${(e as Error).message}. Is the API running?`} />;
  }
  const s = data.summary;
  const hoursPerFte = 140 * 0.8;

  return (
    <div className="mx-auto max-w-[1280px] space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Feature P&L</h1>
          <p className="text-sm text-ink-3">
            {data.period} · monthly averages over the trailing {data.window} month{data.window > 1 ? "s" : ""}
          </p>
        </div>
        <Suspense>
          <div className="flex flex-wrap gap-2">
            <Segmented param="model" value={model} options={[
              { value: "usage_weighted", label: "Usage-weighted" },
              { value: "tier_gated", label: "Tier-gated" },
              { value: "critical_dependency", label: "Revenue at risk" }]} />
            <Segmented param="view" value={view} options={[
              { value: "contribution", label: "Contribution" }, { value: "fully_loaded", label: "Fully loaded" }]} />
            <Segmented param="window" value={win} options={[
              { value: "1", label: "1 mo" }, { value: "3", label: "3 mo" }, { value: "6", label: "6 mo" }]} />
          </div>
        </Suspense>
      </header>

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Kpi label="Feature cost / mo" value={usdShort(s.monthly_cost)} sub={`${s.features} product features`} />
        <Kpi label="Attributed value / mo" value={s.monthly_value == null ? "n/a" : usdShort(s.monthly_value)}
          sub={model === "critical_dependency" ? "counterfactual model is not additive" : "sums to active MRR"} />
        <Kpi label="Zombie tax / mo" tone="bad" value={usdShort(s.zombie_tax_month)}
          sub={`${s.zombie_count} zombies · ${num(s.zombie_eng_hours_month)} eng h ≈ ${(s.zombie_eng_hours_month / hoursPerFte).toFixed(1)} FTE`} />
        <Kpi label="Platform overhead / mo" value={usdShort(s.platform_overhead)}
          sub={view === "fully_loaded" ? "allocated into features" : "not in contribution view"} />
        <Kpi label="Data gaps" value={`${num(s.unmapped_eng_hours)} h`}
          sub={`unmapped eng · ${usdShort(s.untagged_infra_month)} untagged infra`} />
      </section>

      <section className="card p-5">
        <SectionTitle title="Cost vs. value"
          sub="Each dot is a feature. Below the dashed line it costs more than it earns. Click a dot for its tear-sheet." />
        <QuadrantScatter rows={data.features} />
      </section>

      <section>
        <SectionTitle title="Ledger" sub="Sorted by net monthly result, worst first. Hover a class to see why it was assigned." />
        <LedgerTable rows={data.features} />
      </section>
    </div>
  );
}
