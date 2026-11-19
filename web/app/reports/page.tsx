"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { Gated } from "@/components/Auth";
import { ErrorBox } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { num, pct, usdShort } from "@/lib/format";
import type { Digest } from "@/lib/types";

type SlackPayload = { text: string; blocks: { type: string; text?: { text: string }; fields?: { text: string }[]; elements?: { text?: string | { text: string } }[] }[] };

export default function ReportsPage() {
  return <Suspense><Reports /></Suspense>;
}

function Reports() {
  const sp = useSearchParams();
  const [period, setPeriod] = useState(sp?.get("period") ?? "");
  const [d, setD] = useState<Digest | null>(null);
  const [slack, setSlack] = useState<SlackPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiGet<{ summary: Digest; slack: SlackPayload }>(`/api/v1/reports/executive-digest${period ? `?period=${period}` : ""}`)
      .then((r) => { setD(r.summary); setSlack(r.slack); setErr(null); }).catch((e) => setErr(e.message));
  }, [period]);
  useEffect(() => { load(); }, [load]);

  const send = async () => {
    setBusy(true); setNote(null);
    try {
      const r = await apiPost<{ slack_status: string; slack_error: string | null }>("/api/v1/reports/executive-digest",
        { period: period || undefined, post_to_slack: true });
      setNote(r.slack_status === "posted" ? "Posted to Slack." : r.slack_status === "dry_run"
        ? "Dry run (sandbox or demo): stored, not sent. The Slack preview below is exactly what would be posted."
        : `Stored; Slack: ${r.slack_status}${r.slack_error ? ` (${r.slack_error})` : ""}.`);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };

  if (err) return <ErrorBox error={err} />;
  if (!d) return <div className="text-sm text-ink-3">Building the brief…</div>;
  const zt = d.zombie_tax;
  return (
    <div className="mx-auto max-w-[960px] space-y-6 print:max-w-none">
      <div className="flex flex-wrap items-center justify-between gap-3 print:hidden">
        <div className="flex items-center gap-2 text-sm">
          <label htmlFor="period" className="text-ink-3">Month</label>
          <input id="period" type="month" value={period || d.period} onChange={(e) => setPeriod(e.target.value)}
            className="rounded-md border border-rule bg-white px-2 py-1 text-sm" />
        </div>
        <div className="flex gap-2">
          <button className="btn" onClick={() => window.print()}>Download PDF</button>
          <a className="btn" href={`/api/v1/reports/executive-brief.html?period=${d.period}`} target="_blank" rel="noreferrer">Open HTML</a>
          <Gated perm="reports:send" className="btn-primary" disabled={busy} onClick={send}>Send digest to Slack</Gated>
        </div>
      </div>
      {note && <div className="card px-4 py-3 text-sm text-ink-2 print:hidden">{note}</div>}

      <article className="card p-8 print:border-0 print:p-0 print:shadow-none" data-testid="executive-brief">
        <h1 className="text-2xl font-semibold tracking-tight">Executive Brief: feature economics</h1>
        <p className="text-sm text-ink-3">{d.org.name} · {d.period} · ledger through {d.ledger_period}{d.org.demo ? " · live demo data" : ""}</p>
        <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Box label="Zombie tax reclaimed" value={`${usdShort(zt.reclaimed_this_month.cost_month)}/mo`}
            sub={`${num(zt.reclaimed_this_month.eng_hours_month)} h · ${zt.reclaimed_this_month.fte} FTE · ${zt.reclaimed_this_month.sunsets} sunset(s)`} />
          <Box label="Reclaimed to date" value={`${usdShort(zt.reclaimed_to_date.annualized)}/yr`}
            sub={`${zt.reclaimed_to_date.fte} FTE · ${zt.reclaimed_to_date.sunsets} sunset(s)`} />
          <Box label="Zombie tax still running" value={`${usdShort(zt.outstanding.cost_month)}/mo`}
            sub={`${num(zt.outstanding.eng_hours_month)} h · ${zt.outstanding.fte} FTE`} />
          <Box label="AI token spend" value={`${usdShort(d.ai.token_spend_month)}/mo`}
            sub={`${d.ai.margin_bleed_features.length} feature(s) bleeding margin`} />
        </div>
        <h2 className="label mt-8 mb-2">Top bleeding features</h2>
        <table className="w-full text-sm">
          <thead><tr className="border-b border-rule text-left text-xs text-ink-3"><th className="py-1.5">#</th><th>Feature</th><th>Why</th><th className="text-right">Bleed / mo</th><th className="text-right">Gross margin</th><th className="text-right">Adoption</th></tr></thead>
          <tbody>
            {d.top_bleeders.map((b, i) => (
              <tr key={b.key} className="border-b border-rule align-top">
                <td className="py-2">{i + 1}</td>
                <td className="py-2"><b>{b.name}</b><div className="text-xs text-ink-3">{b.reason}</div></td>
                <td className="py-2 text-xs">{b.kind === "margin_bleed" ? "AI margin bleed" : "Net loss"}</td>
                <td className="num py-2 text-right">{usdShort(b.bleed_month)}</td>
                <td className="num py-2 text-right">{pct(b.gross_margin_pct, 0)}</td>
                <td className="num py-2 text-right">{pct(b.adoption, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-8 grid gap-6 md:grid-cols-2">
          <div>
            <h2 className="label mb-2">Sunsets completed</h2>
            <ul className="space-y-1 text-sm">
              {d.sunsets_completed.length ? d.sunsets_completed.map((s) => (
                <li key={s.key}><b>{s.feature}</b>: {usdShort(s.cost_month)}/mo, {num(s.eng_hours_month)} h/mo reclaimed</li>
              )) : <li className="text-ink-3">None this month.</li>}
            </ul>
          </div>
          <div>
            <h2 className="label mb-2">In flight</h2>
            <ul className="space-y-1 text-sm">
              {d.sunsets_in_flight.length ? d.sunsets_in_flight.map((s) => (
                <li key={s.key}>{s.feature}: {s.state.replace(/_/g, " ")} ({s.days_in_state} d)</li>
              )) : <li className="text-ink-3">None.</li>}
            </ul>
          </div>
        </div>
        <h2 className="label mt-8 mb-2">Data health: {d.data_health.status} ({Math.round(d.data_health.score * 100)}%)</h2>
        <ul className="space-y-1 text-sm">
          {d.data_health.checks.map((c) => (
            <li key={c.name} className={c.ok ? "text-ink-2" : "text-[#8a3413]"}>{c.ok ? "✓" : "!"} {c.name}: {c.detail}</li>
          ))}
        </ul>
        <p className="mt-8 border-t border-rule pt-3 text-[11px] text-ink-3">
          Generated {d.generated_at.slice(0, 16).replace("T", " ")} UTC. Engineering cost is aggregated by feature and team; no individual
          developer metrics. Bleed = max(net loss, AI overspend vs. the target gross margin). Reclaimed values are trailing-3-month averages
          frozen when each sunset was archived.
        </p>
      </article>

      {slack && (
        <section className="card p-5 print:hidden">
          <div className="label mb-2">Slack preview (Block Kit)</div>
          <div className="space-y-2 border-l-4 border-[#e7e6e2] pl-3 text-sm">
            {slack.blocks.map((b, i) => {
              if (b.type === "header") return <div key={i} className="font-semibold">{b.text?.text}</div>;
              if (b.type === "divider") return <hr key={i} className="border-rule" />;
              if (b.type === "section") return (
                <div key={i} className="whitespace-pre-wrap">
                  {b.text?.text && <div>{b.text.text.replace(/\*/g, "")}</div>}
                  {b.fields && <div className="grid grid-cols-2 gap-2">{b.fields.map((f, j) => <div key={j}>{f.text.replace(/\*/g, "")}</div>)}</div>}
                </div>);
              if (b.type === "context") return <div key={i} className="text-xs text-ink-3">{b.elements?.map((e) => (typeof e.text === "string" ? e.text : "")).join(" ")}</div>;
              if (b.type === "actions") return <div key={i} className="flex gap-2">{b.elements?.map((e, j) => <span key={j} className="btn text-xs">{typeof e.text === "object" ? e.text.text : ""}</span>)}</div>;
              return null;
            })}
          </div>
          <details className="mt-3 text-xs"><summary className="cursor-pointer text-ink-3">Raw payload</summary>
            <pre className="mt-2 max-h-72 overflow-auto rounded-md bg-[#f6f5f2] p-3">{JSON.stringify(slack, null, 2)}</pre></details>
        </section>
      )}
    </div>
  );
}

function Box({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="rounded-lg border border-rule px-3 py-2.5">
      <div className="label">{label}</div>
      <div className="num mt-0.5 text-xl font-semibold">{value}</div>
      <div className="text-[11px] text-ink-3">{sub}</div>
    </div>
  );
}
