"use client";

import { useState } from "react";
import { usd, usdShort } from "@/lib/format";
import type { PnlMonth } from "@/lib/types";

// Three stacked cost parts use categorical slots 1-3; attributed value is a dark ink line on the same $ axis.
const PARTS = [
  { key: "c_eng", label: "Engineering", color: "#2a78d6" },
  { key: "c_support", label: "Support", color: "#eb6834" },
  { key: "c_infra", label: "Infra + AI", color: "#1baf7a" },
] as const;
const W = 720, H = 260, M = { l: 56, r: 16, t: 12, b: 28 };

export function CostTrend({ history }: { history: PnlMonth[] }) {
  const [hi, setHi] = useState<number | null>(null);
  const max = Math.max(...history.map((h) => Math.max(h.c_total, h.v_usage)), 1) * 1.1;
  const step = (W - M.l - M.r) / history.length;
  const bw = Math.min(46, step * 0.55);
  const y = (v: number) => H - M.b - (v / max) * (H - M.t - M.b);
  const ticks = Array.from({ length: 5 }, (_, i) => (max / 4) * i);
  const cx = (i: number) => M.l + step * i + step / 2;
  const line = history.map((h, i) => `${i ? "L" : "M"}${cx(i)} ${y(h.v_usage)}`).join(" ");

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Monthly cost by type and attributed value">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} stroke="#ecebe7" />
            <text x={M.l - 8} y={y(t) + 4} textAnchor="end" fontSize={11} fill="#8a8984">{usdShort(t)}</text>
          </g>
        ))}
        {history.map((h, i) => {
          let base = 0;
          return (
            <g key={h.period_month} onMouseEnter={() => setHi(i)} onMouseLeave={() => setHi(null)}>
              <rect x={cx(i) - step / 2} y={M.t} width={step} height={H - M.t - M.b} fill={hi === i ? "#f3f2ee" : "transparent"} />
              {PARTS.map((p, j) => {
                const v = h[p.key];
                const top = y(base + v), bottom = y(base);
                base += v;
                const hgt = Math.max(bottom - top - (j ? 2 : 0), 0);
                return <rect key={p.key} x={cx(i) - bw / 2} y={top} width={bw} height={hgt} rx={j === PARTS.length - 1 ? 4 : 0} fill={p.color} />;
              })}
              <text x={cx(i)} y={H - 8} textAnchor="middle" fontSize={11} fill="#8a8984">{h.period_month.slice(0, 7)}</text>
            </g>
          );
        })}
        <path d={line} fill="none" stroke="#0b0b0b" strokeWidth={2} />
        {history.map((h, i) => <circle key={i} cx={cx(i)} cy={y(h.v_usage)} r={4} fill="#0b0b0b" stroke="#fcfcfb" strokeWidth={2} />)}
        {history.length > 0 && (
          <text x={cx(history.length - 1) + 8} y={y(history[history.length - 1].v_usage) - 8} fontSize={11.5} fill="#0b0b0b" textAnchor="end">
            Attributed value
          </text>
        )}
      </svg>
      <div className="mt-1 flex gap-4 px-2 text-xs text-ink-2">
        {PARTS.map((p) => (
          <span key={p.key} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: p.color }} />{p.label}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-4 bg-ink" />Attributed value</span>
      </div>
      {hi !== null && (
        <div className="pointer-events-none absolute top-2 z-10 w-52 rounded-lg border border-rule bg-white p-3 text-xs shadow-lg"
          style={{ left: `${Math.min((cx(hi) / W) * 100, 70)}%`, transform: "translateX(12px)" }}>
          <div className="mb-1.5 font-semibold">{history[hi].period_month.slice(0, 7)}</div>
          {PARTS.map((p) => (
            <div key={p.key} className="flex justify-between py-0.5"><span className="text-ink-3">{p.label}</span>
              <span className="num">{usd(history[hi][p.key])}</span></div>
          ))}
          <div className="flex justify-between border-t border-rule py-0.5 pt-1"><span className="text-ink-3">Total cost</span>
            <span className="num font-medium">{usd(history[hi].c_total)}</span></div>
          <div className="flex justify-between py-0.5"><span className="text-ink-3">Value</span>
            <span className="num font-medium">{usd(history[hi].v_usage)}</span></div>
        </div>
      )}
    </div>
  );
}
