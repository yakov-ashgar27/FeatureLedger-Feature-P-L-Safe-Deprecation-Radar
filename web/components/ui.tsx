import { CLASS_META } from "@/lib/format";
import type { FeatureClass } from "@/lib/types";

export function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "bad" | "good" }) {
  return (
    <div className="card px-4 py-3.5">
      <div className="label">{label}</div>
      <div className={`num mt-1 text-[22px] font-semibold tracking-tight ${tone === "bad" ? "text-[#b4471c]" : ""}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-ink-3">{sub}</div>}
    </div>
  );
}

export function ClassBadge({ value }: { value: FeatureClass }) {
  const m = CLASS_META[value];
  return (
    <span title={m.hint} className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-rule bg-white px-2 py-0.5 text-xs text-ink-2">
      <span className={`h-2 w-2 rounded-full ${m.dot}`} />
      {m.label}
    </span>
  );
}

export function SectionTitle({ title, sub, right }: { title: string; sub?: string; right?: React.ReactNode }) {
  return (
    <div className="mb-3 flex items-end justify-between gap-4">
      <div>
        <h2 className="text-[15px] font-semibold">{title}</h2>
        {sub && <p className="text-xs text-ink-3">{sub}</p>}
      </div>
      {right}
    </div>
  );
}

export function ErrorBox({ error }: { error: string }) {
  return <div className="card border-[#f1c3b0] bg-[#fdf3ee] px-4 py-3 text-sm text-[#8a3413]">{error}</div>;
}
