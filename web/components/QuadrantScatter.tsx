"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { CLASS_META, pct, usdShort } from "@/lib/format";
import type { LedgerRow } from "@/lib/types";

// Categorical slots 1-3 validated all-pairs (scatter): blue / orange / aqua. Everything else folds to neutral.
const COLOR: Record<string, string> = { cash_cow: "#2a78d6", zombie: "#eb6834", niche_anchor: "#1baf7a" };
const NEUTRAL = "#b4b3ad";
const W = 760, H = 430, M = { l: 64, r: 24, t: 18, b: 46 };

const log = (v: number) => Math.log10(Math.max(v, 100));

export function QuadrantScatter({ rows }: { rows: LedgerRow[] }) {
  const router = useRouter();
  const [hover, setHover] = useState<LedgerRow | null>(null);
  const pts = rows.filter((r) => !r.is_shared_overhead);

  const { x, y, ticks } = useMemo(() => {
    const vals = pts.flatMap((r) => [r.c_total, r.value]).filter((v) => v > 0);
    const lo = Math.floor(log(Math.min(...vals, 1000)));
    const hi = Math.ceil(log(Math.max(...vals, 10000)));
    const x = (v: number) => M.l + ((log(v) - lo) / (hi - lo)) * (W - M.l - M.r);
    const y = (v: number) => H - M.b - ((log(v) - lo) / (hi - lo)) * (H - M.t - M.b);
    const ticks = Array.from({ length: hi - lo + 1 }, (_, i) => 10 ** (lo + i));
    return { x, y, ticks };
  }, [pts]);

  const labelled = new Set(
    [...pts.filter((r) => r.classification === "zombie" || r.classification === "niche_anchor"),
     ...[...pts].sort((a, b) => b.value - a.value).slice(0, 3)].map((r) => r.key),
  );
  const lo = ticks[0], hi = ticks[ticks.length - 1];

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
        aria-label="Monthly cost versus attributed monthly value per feature, log scales">
        {/* losing-money region: below break-even */}
        <path d={`M${x(lo)} ${y(lo)} L${x(hi)} ${y(hi)} L${x(hi)} ${H - M.b} L${x(lo)} ${H - M.b} Z`} fill="#eb6834" opacity={0.05} />
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={M.t} y2={H - M.b} stroke="#ecebe7" />
            <line x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} stroke="#ecebe7" />
            <text x={x(t)} y={H - M.b + 16} textAnchor="middle" fontSize={11} fill="#8a8984">{usdShort(t)}</text>
            <text x={M.l - 8} y={y(t) + 4} textAnchor="end" fontSize={11} fill="#8a8984">{usdShort(t)}</text>
          </g>
        ))}
        <line x1={x(lo)} y1={y(lo)} x2={x(hi)} y2={y(hi)} stroke="#8a8984" strokeDasharray="4 4" />
        <text x={x(hi) - 6} y={y(hi) + 16} textAnchor="end" fontSize={11} fill="#52514e">break-even (value = cost)</text>
        <text x={x(hi) - 6} y={H - M.b - 8} textAnchor="end" fontSize={11} fill="#8a8984">costs more than it earns</text>
        <text x={(M.l + W - M.r) / 2} y={H - 6} textAnchor="middle" fontSize={12} fill="#52514e">Monthly direct cost (eng + support + infra)</text>
        <text transform={`translate(14 ${(M.t + H - M.b) / 2}) rotate(-90)`} textAnchor="middle" fontSize={12} fill="#52514e">Attributed monthly value</text>

        {pts.map((r) => {
          const cx = x(r.c_total), cy = y(r.value);
          const c = COLOR[r.classification] ?? NEUTRAL;
          return (
            <g key={r.key} className="cursor-pointer" onMouseEnter={() => setHover(r)} onMouseLeave={() => setHover(null)}
              onClick={() => router.push(`/features/${r.key}`)}>
              <circle cx={cx} cy={cy} r={14} fill="transparent" />
              <circle cx={cx} cy={cy} r={hover?.key === r.key ? 7 : 5.5} fill={c} stroke="#fcfcfb" strokeWidth={2} />
              {labelled.has(r.key) && (
                <text x={cx + 9} y={cy + 4} fontSize={11.5} fill="#0b0b0b">{r.name}</text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="mt-1 flex flex-wrap gap-4 px-2 text-xs text-ink-2">
        {(["cash_cow", "zombie", "niche_anchor"] as const).map((k) => (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: COLOR[k] }} />{CLASS_META[k].label}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full" style={{ background: NEUTRAL }} />Other (growth, watch, mandated)
        </span>
      </div>
      {hover && (
        <div className="pointer-events-none absolute z-10 w-64 rounded-lg border border-rule bg-white p-3 text-xs shadow-lg"
          style={{ left: `${Math.min((x(hover.c_total) / W) * 100, 62)}%`, top: `${(y(hover.value) / H) * 100}%`,
                   transform: "translate(12px, -50%)" }}>
          <div className="mb-1 font-semibold text-ink">{hover.name}</div>
          <div className="mb-2 text-ink-3">{CLASS_META[hover.classification].label} · {hover.team}</div>
          <Row k="Direct cost / mo" v={usdShort(hover.c_total)} />
          <Row k="Attributed value / mo" v={usdShort(hover.value)} />
          <Row k="Net / mo" v={usdShort(hover.net_roi)} />
          <Row k="Adoption" v={pct(hover.adoption)} />
          <Row k="Bug tax" v={pct(hover.bug_tax_ratio, 0)} />
        </div>
      )}
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3 py-0.5">
      <span className="text-ink-3">{k}</span>
      <span className="num font-medium text-ink">{v}</span>
    </div>
  );
}
