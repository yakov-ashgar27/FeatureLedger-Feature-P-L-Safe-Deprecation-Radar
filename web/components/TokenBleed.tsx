import Link from "next/link";
import { pct, usdShort } from "@/lib/format";
import type { AiMarginFeature, AiTier } from "@/lib/types";

// Token spend (slot 4, yellow) next to the revenue the feature earns on each plan tier (slot 1, blue).
const TOKENS = "#eda100";
const REVENUE = "#2a78d6";
const TIER_ORDER = ["free", "starter", "pro", "business", "enterprise", "unknown"];

export function MarginBleedBadge({ reason }: { reason?: string | null }) {
  return (
    <span title={reason ?? "Popular feature whose AI token spend leaves too little gross margin"}
      className="inline-flex items-center gap-1 rounded-full bg-[#fff4d6] px-2 py-0.5 text-[11px] font-medium text-[#7a5300]">
      <span aria-hidden>▲</span> margin bleed
    </span>
  );
}

export function TierBars({ tiers }: { tiers: Record<string, AiTier> }) {
  const rows = Object.entries(tiers).sort(([a], [b]) => TIER_ORDER.indexOf(a) - TIER_ORDER.indexOf(b));
  const max = Math.max(1, ...rows.flatMap(([, t]) => [t.tokens_usd, t.revenue_usd]));
  return (
    <div>
      <div className="mb-2 flex gap-4 text-[11px] text-ink-3">
        <span className="flex items-center gap-1.5"><span className="h-2 w-3 rounded-sm" style={{ background: TOKENS }} />AI tokens / mo</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-3 rounded-sm" style={{ background: REVENUE }} />Attributed revenue / mo</span>
      </div>
      <div className="space-y-2.5">
        {rows.map(([tier, t]) => (
          <div key={tier} className="grid grid-cols-[72px_1fr_96px] items-center gap-2 text-xs"
            title={`${tier}: ${t.accounts} active account(s), ${(t.token_share * 100).toFixed(0)}% of usage events`}>
            <div className="capitalize text-ink-2">{tier}</div>
            <div className="space-y-[2px]">
              <div className="h-2 rounded-r-[4px]" style={{ width: `${Math.max(1, (100 * t.tokens_usd) / max)}%`, background: TOKENS }} />
              <div className="h-2 rounded-r-[4px]" style={{ width: `${Math.max(t.revenue_usd ? 1 : 0, (100 * t.revenue_usd) / max)}%`, background: REVENUE }} />
            </div>
            <div className="num text-right text-ink-2">{usdShort(t.tokens_usd)} / {usdShort(t.revenue_usd)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function TokenBleedRadar({ features, maxGm }: { features: AiMarginFeature[]; maxGm: number }) {
  if (!features.length) return <div className="card p-5 text-sm text-ink-3">No feature has AI/LLM spend tagged to it.</div>;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {features.map((f) => {
        const cur = f.history[f.history.length - 1];
        const gm = cur?.gross_margin_pct ?? null;
        return (
          <div key={f.key} className="card p-5" data-testid={`radar-${f.key}`}>
            <div className="flex items-start justify-between gap-3">
              <div>
                <Link href={`/features/${f.key}`} className="font-medium hover:underline">{f.name}</Link>
                <div className="text-xs text-ink-3">{pct(cur?.adoption, 0)} adoption · {usdShort(cur?.c_ai_tokens)}/mo tokens · {usdShort(cur?.c_infra)}/mo infra</div>
              </div>
              {f.margin_bleed && <MarginBleedBadge reason={f.margin_bleed_reason} />}
            </div>
            <div className="mt-3 flex items-end gap-6">
              <div>
                <div className="label">Gross margin</div>
                <div className={`num text-2xl font-semibold ${gm != null && gm < maxGm ? "text-[#b4471c]" : ""}`}>{pct(gm, 0)}</div>
              </div>
              <div className="flex h-10 items-end gap-1" aria-label="Gross margin by month">
                {f.history.map((h) => {
                  const v = h.gross_margin_pct ?? 0;
                  return <div key={h.period} title={`${h.period}: ${pct(h.gross_margin_pct, 0)} gross margin`}
                    className="w-3 rounded-t-[3px]" style={{ height: `${Math.max(3, Math.min(100, Math.abs(v) * 100))}%`,
                      background: v < maxGm ? "#e34948" : "#b4b3ad" }} />;
                })}
              </div>
              <div className="pb-1 text-[11px] text-ink-3">last {f.history.length} months<br />red = below {pct(maxGm, 0)}</div>
            </div>
            {f.margin_bleed_reason && <p className="mt-3 text-xs text-ink-2">{f.margin_bleed_reason}</p>}
            <div className="mt-4 border-t border-rule pt-3"><TierBars tiers={f.tiers} /></div>
          </div>
        );
      })}
    </div>
  );
}
