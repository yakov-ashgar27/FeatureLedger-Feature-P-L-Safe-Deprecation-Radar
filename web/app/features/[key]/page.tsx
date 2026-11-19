import Link from "next/link";
import { CostTrend } from "@/components/CostTrend";
import { WhyThisNumber } from "@/components/Confidence";
import { MarginBleedBadge, TierBars } from "@/components/TokenBleed";
import { StartSunset } from "@/components/StartSunset";
import { ClassBadge, ErrorBox, Kpi, SectionTitle } from "@/components/ui";
import { serverLoad } from "@/lib/server";
import { num, pct, STATE_LABEL, usd, usdShort } from "@/lib/format";
import type { TearSheet } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function FeaturePage({ params }: { params: Promise<{ key: string }> }) {
  const { key } = await params;
  const res = await serverLoad<TearSheet>(`/api/v1/features/${encodeURIComponent(key)}`, `/features/${key}`);
  if (res.error !== null) return <ErrorBox error={res.error} />;
  const d = res.data;
  const f = d.feature;
  const cur = d.history[d.history.length - 1];
  const first = d.history[0];
  const canSunset = !f.is_regulatory && !f.is_shared_overhead && !d.sunset_plan;
  const net = cur ? cur.v_usage - cur.c_total : 0;

  return (
    <div className="mx-auto max-w-[1180px] space-y-6">
      <Link href="/" className="text-xs text-ink-3 hover:underline">← Feature P&L</Link>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-2xl">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">{f.name}</h1>
            {cur && <ClassBadge value={cur.classification} />}
            {cur?.margin_bleed && <MarginBleedBadge reason={cur.margin_bleed_reason} />}
          </div>
          <p className="mt-1 text-sm text-ink-3">
            {f.team} · <code className="text-xs">{f.key}</code>
            {f.flag_key && <> · flag <code className="text-xs">{f.flag_key}</code></>}
            {f.tier_gate && <> · {f.tier_gate} tier</>}
            {f.launched_at && <> · since {f.launched_at.slice(0, 7)}</>}
          </p>
          {f.description && <p className="mt-2 text-sm text-ink-2">{f.description}</p>}
          {cur && <p className="mt-2 text-sm text-ink-2"><span className="font-medium text-ink">Why: </span>{cur.classification_reason}</p>}
        </div>
        {d.sunset_plan ? (
          <Link href={`/sunset?plan=${d.sunset_plan.id}`} className="btn">
            Sunset plan: {STATE_LABEL[d.sunset_plan.state]} →
          </Link>
        ) : canSunset ? (
          <StartSunset featureKey={f.key} defaultReason={cur?.classification_reason ?? ""} />
        ) : (
          <span className="text-xs text-ink-3">{f.is_regulatory ? "Regulatory: cannot be sunset" : "Shared overhead"}</span>
        )}
      </header>

      {cur && (
        <section className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <Kpi label="Direct cost / mo" value={usdShort(cur.c_total)} sub={`${num(cur.eng_hours)} eng hours`} />
          <Kpi label="Attributed value / mo" value={usdShort(cur.v_usage)} sub={`tier-gated ${usdShort(cur.v_tier)}`} />
          <Kpi label="Net / mo" value={usdShort(net)} tone={net < 0 ? "bad" : undefined}
            sub={`efficiency ${(cur.v_usage / Math.max(cur.c_total, 1)).toFixed(2)}×`} />
          <Kpi label="Adoption" value={pct(cur.eligible_accounts ? cur.active_accounts / cur.eligible_accounts : 0)}
            sub={`${cur.active_accounts} of ${cur.eligible_accounts} eligible paying accounts${first && first !== cur ? ` (was ${first.active_accounts})` : ""}`} />
          <Kpi label="ARR at risk if removed" value={usdShort(cur.revenue_at_risk_arr)}
            sub={`${cur.anchor_accounts} anchor account(s) · bug tax ${pct(cur.bug_tax_ratio, 0)}`} />
        </section>
      )}

      {cur && d.history.some((h) => h.c_ai_tokens > 0) && (
        <section className="card p-5" data-testid="ai-margin">
          <SectionTitle title="AI gross margin"
            sub="Variable AI/LLM token spend and fixed cloud infra against attributed revenue. Gross margin = (value − AI tokens − infra) / value." />
          <div className="grid gap-6 lg:grid-cols-2">
            <div>
              <div className="grid grid-cols-3 gap-3">
                <Kpi label="AI tokens / mo" value={usdShort(cur.c_ai_tokens)} sub={`${pct(cur.c_ai_tokens / Math.max(cur.v_usage, 1), 0)} of value`} />
                <Kpi label="Cloud infra / mo" value={usdShort(cur.c_infra)} sub="fixed" />
                <Kpi label="Gross margin" value={pct(cur.gross_margin_pct, 0)} tone={cur.margin_bleed ? "bad" : undefined}
                  sub={cur.margin_bleed ? "margin bleed" : "healthy"} />
              </div>
              {cur.margin_bleed_reason && <p className="mt-3 text-sm text-ink-2">{cur.margin_bleed_reason}</p>}
              <table className="mt-4 w-full text-xs">
                <thead><tr className="text-left text-ink-3"><th className="py-1">Month</th><th className="text-right">Tokens</th><th className="text-right">Value</th><th className="text-right">Gross margin</th></tr></thead>
                <tbody>{d.history.map((h) => (
                  <tr key={h.period_month} className="border-t border-rule">
                    <td className="py-1">{h.period_month.slice(0, 7)}</td>
                    <td className="num text-right">{usdShort(h.c_ai_tokens)}</td>
                    <td className="num text-right">{usdShort(h.v_usage)}</td>
                    <td className={`num text-right ${h.margin_bleed ? "font-medium text-[#b4471c]" : ""}`}>{pct(h.gross_margin_pct, 0)}</td>
                  </tr>))}</tbody>
              </table>
            </div>
            <div>
              <div className="label mb-2">Token burn vs. revenue by plan tier (latest month)</div>
              <TierBars tiers={cur.ai_tier_breakdown ?? {}} />
              <p className="mt-3 text-[11px] text-ink-3">Tokens are spread over tiers by usage events; revenue is the usage-weighted attribution.</p>
            </div>
          </div>
        </section>
      )}

      <section className="card p-5">
        <SectionTitle title="Why this number?" sub="How every line is computed, from which data, and how much of it is measured rather than modelled" />
        <WhyThisNumber featureKey={key} />
      </section>

      <section className="card p-5">
        <SectionTitle title="Cost and value by month" sub="Direct cost stacked by type; the line is usage-weighted attributed value" />
        <CostTrend history={d.history} />
      </section>

      <div className="grid gap-6 lg:grid-cols-5">
        <section className="lg:col-span-3">
          <SectionTitle title="Paying accounts using it" sub="Latest month, by ARR. Dependency = share of the account's active days that touch this feature." />
          <div className="card overflow-hidden">
            <table className="w-full text-sm">
              <thead className="border-b border-rule bg-[#f9f8f6]">
                <tr><th className="px-3 py-2">Account</th><th className="px-3 py-2 text-right">ARR</th>
                  <th className="px-3 py-2 text-right">Days used</th><th className="px-3 py-2 text-right">Dependency</th>
                  <th className="px-3 py-2 text-right">Churn risk</th><th className="px-3 py-2 text-right">Renewal</th></tr>
              </thead>
              <tbody>
                {d.accounts.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-ink-3">No paying account used it this month.</td></tr>}
                {d.accounts.slice(0, 25).map((a) => (
                  <tr key={a.id} className="border-b border-rule last:border-0">
                    <td className="px-3 py-2"><div className="font-medium">{a.name}</div>
                      <div className="text-xs text-ink-3">{a.plan_tier} · {a.health}</div></td>
                    <td className="num px-3 py-2 text-right">{usd(a.arr_usd)}</td>
                    <td className="num px-3 py-2 text-right">{a.feature_active_days}/{a.account_active_days}</td>
                    <td className="num px-3 py-2 text-right">{pct(a.dependency_score, 0)}</td>
                    <td className={`num px-3 py-2 text-right ${a.churn_risk >= 0.5 ? "font-semibold text-[#b4471c]" : ""}`}>{pct(a.churn_risk, 0)}</td>
                    <td className="num px-3 py-2 text-right text-ink-2">{a.renewal_date ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section className="space-y-6 lg:col-span-2">
          <div>
            <SectionTitle title="Top bug-generating files" sub="Last 6 months, bug/hotfix/incident PRs" />
            <div className="card divide-y divide-rule">
              {d.top_bug_files.length === 0 && <div className="px-3 py-4 text-sm text-ink-3">No bug PRs touched this feature.</div>}
              {d.top_bug_files.map((b) => (
                <div key={b.path} className="flex items-center justify-between gap-3 px-3 py-2">
                  <code className="truncate text-xs" title={b.path}>{b.path}</code>
                  <span className="num shrink-0 text-xs text-ink-2">{b.bug_prs} PRs · {num(b.churn)} lines</span>
                </div>
              ))}
            </div>
          </div>
          <div>
            <SectionTitle title="Mapped assets" sub="Feature Graph: what resolves to this feature" />
            <div className="card divide-y divide-rule">
              {d.assets.map((a) => (
                <div key={a.asset_type + a.identifier} className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                  <span className="w-24 shrink-0 text-ink-3">{a.asset_type.replace("_", " ")}</span>
                  <code className="flex-1 truncate">{a.identifier}</code>
                  <span className="text-ink-3">{a.source} · {Math.round(a.confidence * 100)}%</span>
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
