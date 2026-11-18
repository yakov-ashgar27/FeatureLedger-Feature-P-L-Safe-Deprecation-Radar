"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { ErrorBox, SectionTitle } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { pct, STATE_LABEL, usd, usdShort } from "@/lib/format";
import type { SunsetPlan, SunsetState } from "@/lib/types";

const COLUMNS: SunsetState[] = ["candidate_for_sunset", "blast_radius_checked", "customer_outreach",
  "canary_dark_launch", "dead_code_pr_opened", "archived"];

export default function Page() {
  return <Suspense><SunsetBoard /></Suspense>;
}

function SunsetBoard() {
  const sp = useSearchParams();
  const router = useRouter();
  const selected = sp?.get("plan") ?? null;
  const [plans, setPlans] = useState<SunsetPlan[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => apiGet<SunsetPlan[]>("/api/v1/sunset-plans").then(setPlans).catch((e) => setErr(e.message)), []);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="mx-auto max-w-[1400px] space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Safe Sunset pipeline</h1>
        <p className="text-sm text-ink-3">Each step is gated by evidence. The feature flag stays as the rollback lever until the dead code is merged.</p>
      </header>
      {err && <ErrorBox error={err} />}
      <div className="grid grid-cols-6 gap-3">
        {COLUMNS.map((c, i) => (
          <div key={c} className="min-w-0">
            <div className="label mb-2 flex items-center gap-1.5"><span className="num text-ink-2">{i + 1}</span>{STATE_LABEL[c]}</div>
            <div className="space-y-2">
              {plans.filter((p) => p.state === c).map((p) => (
                <button key={p.id} onClick={() => router.push(`/sunset?plan=${p.id}`)}
                  className={`card block w-full p-3 text-left hover:border-ink-3 ${selected === p.id ? "border-ink" : ""}`}>
                  <div className="truncate text-sm font-medium">{p.feature_name}</div>
                  <div className="mt-1 text-xs text-ink-3">{p.days_in_state} d in step{p.canary_phase ? ` · ${p.canary_phase.replace(/_/g, " ")}` : ""}</div>
                  {p.metrics && <div className="num mt-1 text-xs text-ink-2">{usdShort(p.metrics.c_total)}/mo · {Math.round(p.metrics.eng_hours)} h</div>}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
      {plans.some((p) => p.state === "cancelled") && (
        <div className="text-xs text-ink-3">Cancelled: {plans.filter((p) => p.state === "cancelled").map((p) => p.feature_name).join(", ")}</div>
      )}
      {selected && <PlanDetail id={selected} onChange={load} />}
    </div>
  );
}

function PlanDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const [p, setP] = useState<SunsetPlan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [csSignoff, setCsSignoff] = useState(false);
  const [prMerged, setPrMerged] = useState(false);
  const [openEmail, setOpenEmail] = useState<string | null>(null);

  const load = useCallback(() => apiGet<SunsetPlan>(`/api/v1/sunset-plans/${id}`).then(setP).catch((e) => setErr(e.message)), [id]);
  useEffect(() => { setErr(null); load(); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    try { await fn(); await load(); onChange(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  if (!p) return null;
  const terminal = p.state === "archived" || p.state === "cancelled";
  const step = COLUMNS.indexOf(p.state);

  return (
    <section className="card p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">{p.feature_name}</h2>
          <p className="text-sm text-ink-3">{STATE_LABEL[p.state]} · flag <code className="text-xs">{p.flag_key ?? "none"}</code> · scream test {p.scream_test_days} days</p>
          <p className="mt-1 max-w-2xl text-sm text-ink-2">{p.reason}</p>
        </div>
        <Link href={`/features/${p.feature_key}`} className="btn">Tear-sheet →</Link>
      </div>

      <ol className="my-5 flex gap-1">
        {COLUMNS.map((c, i) => (
          <li key={c} className={`h-1.5 flex-1 rounded-full ${p.state === "cancelled" ? "bg-[#e7e6e2]" : i <= step ? "bg-ink" : "bg-[#e7e6e2]"}`} title={STATE_LABEL[c]} />
        ))}
      </ol>

      {err && <div className="mb-4"><ErrorBox error={err} /></div>}

      {!terminal && (
        <div className="mb-6 flex flex-wrap items-center gap-2">
          <button className="btn" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/evidence/blast_radius`))}>1 · Compute blast radius</button>
          <button className="btn" disabled={busy || !p.blast_radius} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/acknowledge`, { kind: "inbound", ids: ["*"] }))}>Acknowledge call sites</button>
          <button className="btn" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/evidence/impact_dossier`))}>2 · Build account dossier</button>
          <button className="btn" disabled={busy || !p.impact_dossier} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/acknowledge`, { kind: "account", ids: ["*"] }))}>Mark accounts contacted</button>
          <button className="btn" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/evidence/cleanup_pr`))}>4 · Draft cleanup PR</button>
          <span className="mx-2 h-5 w-px bg-rule" />
          <label className="flex items-center gap-1.5 text-xs text-ink-2"><input type="checkbox" checked={csSignoff} onChange={(e) => setCsSignoff(e.target.checked)} className="accent-black" />CS sign-off</label>
          <label className="flex items-center gap-1.5 text-xs text-ink-2"><input type="checkbox" checked={prMerged} onChange={(e) => setPrMerged(e.target.checked)} className="accent-black" />PR merged</label>
          <button className="btn-primary" disabled={busy}
            onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/advance`, { expected_version: p.version, cs_signoff: csSignoff, pr_merged: prMerged }))}>
            {p.state === "canary_dark_launch" && p.canary_phase === "zero_usage_off" ? "Turn flag fully off" : "Advance →"}
          </button>
          {(p.state === "canary_dark_launch" || p.state === "dead_code_pr_opened") && (
            <button className="btn" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/rollback`, { reason: "Manual rollback from dashboard" }))}>Roll back (flag on)</button>
          )}
          <button className="btn" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/cancel`, { reason: "Cancelled from dashboard" }))}>Cancel plan</button>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <div>
          <SectionTitle title="Technical blast radius" sub="Code outside the feature that still imports it" />
          {p.blast_radius ? (
            <div className="card divide-y divide-rule">
              <div className="px-3 py-2 text-xs text-ink-2">{p.blast_radius.inbound_count} inbound reference(s) · risk {p.blast_radius.risk}</div>
              {p.blast_radius.inbound_references.map((r) => (
                <div key={r.from_path} className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                  <code className="truncate">{r.from_path}</code>
                  <span className={r.acknowledged ? "text-ink-3" : "font-medium text-[#b4471c]"}>{r.owner_feature} · {r.acknowledged ? "acknowledged" : "open"}</span>
                </div>
              ))}
            </div>
          ) : <Empty text="Not computed yet." />}
        </div>
        <div>
          <SectionTitle title="Account impact dossier" sub="Paying accounts that used it in the last 90 days, by ARR" />
          {p.impact_dossier ? (
            <div className="card divide-y divide-rule">
              <div className="px-3 py-2 text-xs text-ink-2">
                {p.impact_dossier.accounts_affected} accounts · {usd(p.impact_dossier.arr_touched_usd)} ARR touched · {usd(p.impact_dossier.arr_at_risk_usd)} risk-weighted
                {p.impact_dossier.replacement && <> · migrate to <b>{p.impact_dossier.replacement}</b></>}
              </div>
              {p.impact_dossier.accounts.slice(0, 12).map((a) => (
                <div key={a.account_id} className="px-3 py-2 text-xs">
                  <div className="flex items-center justify-between gap-3">
                    <button className="truncate text-left font-medium hover:underline" onClick={() => setOpenEmail(openEmail === a.account_id ? null : a.account_id)}>{a.name}</button>
                    <span className="num text-ink-2">{usd(a.arr_usd)} · risk <span className={a.churn_risk >= 0.5 ? "font-semibold text-[#b4471c]" : ""}>{pct(a.churn_risk, 0)}</span> · {a.acknowledged ? "contacted" : "open"}</span>
                  </div>
                  {openEmail === a.account_id && <pre className="mt-2 whitespace-pre-wrap rounded-md bg-[#f6f5f2] p-3 font-sans text-xs text-ink-2">{a.email_draft}</pre>}
                </div>
              ))}
            </div>
          ) : <Empty text="Not generated yet." />}
        </div>
        <div>
          <SectionTitle title="Dead-code PR" sub="Outline to open once the scream test is quiet" />
          {p.cleanup_pr ? (
            <div className="card p-3">
              <div className="text-sm font-medium">{p.cleanup_pr.title}</div>
              <div className="mb-2 text-xs text-ink-3">branch <code>{p.cleanup_pr.branch}</code> · {p.cleanup_pr.files_to_delete.length} files to delete · {p.cleanup_pr.files_to_edit.length} to edit</div>
              <pre className="max-h-72 overflow-auto rounded-md bg-[#f6f5f2] p-3 text-[11px] leading-relaxed">{p.cleanup_pr.body_markdown}</pre>
            </div>
          ) : <Empty text="Not drafted yet." />}
        </div>
        <div>
          <SectionTitle title="Audit trail" sub="Append-only, hash-chained" />
          <div className="card max-h-80 divide-y divide-rule overflow-y-auto">
            {(p.audit_log ?? []).map((l) => (
              <div key={l.id} className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                <span><span className="text-ink-3">{l.created_at.slice(0, 16).replace("T", " ")}</span> · {l.action} → {STATE_LABEL[l.to_state] ?? l.to_state}</span>
                <code className="text-ink-3" title={l.hash}>{l.hash.slice(0, 10)}</code>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="card px-3 py-6 text-center text-sm text-ink-3">{text}</div>;
}
