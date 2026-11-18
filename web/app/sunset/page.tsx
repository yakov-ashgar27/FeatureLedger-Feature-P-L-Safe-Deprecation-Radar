"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { Gated, ROLE_LABEL, useCan, useMe } from "@/components/Auth";
import { ErrorBox, SectionTitle } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { pct, STATE_LABEL, usd, usdShort } from "@/lib/format";
import type { FlagStatus, Role, SunsetPlan, SunsetState } from "@/lib/types";

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
        <p className="text-sm text-ink-3">Each step is gated by evidence and by role. FeatureLedger drives the live feature flag through the canary, opens the cleanup PR itself, and archives the feature when that PR is merged. Rollback turns the flag fully on again.</p>
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
                  {p.flag && p.flag.state !== "fully_on" && <div className="mt-1"><FlagPill flag={p.flag} /></div>}
                  {p.pull_request && <div className="mt-1 text-xs text-ink-2">PR #{p.pull_request.number} · {p.pull_request.state}</div>}
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
  const can = useCan();
  const me = useMe();
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
        <div className="mb-6 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Gated perm="sunset:blast_radius" disabled={busy || step > 0} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/evidence/blast_radius`))}>1 · Compute blast radius</Gated>
            <Gated perm="sunset:blast_radius" disabled={busy || !p.blast_radius || step > 0} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/acknowledge`, { kind: "inbound", ids: ["*"] }))}>Acknowledge call sites</Gated>
            <Gated perm="sunset:dossier" disabled={busy || step > 1} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/evidence/impact_dossier`))}>2 · Build account dossier</Gated>
            <Gated perm="sunset:cs_signoff" disabled={busy || !p.impact_dossier || step > 2} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/acknowledge`, { kind: "account", ids: ["*"] }))}>Mark accounts contacted</Gated>
            {p.state === "blast_radius_checked" && (
              <label className="flex items-center gap-1.5 text-xs text-ink-2" title={can("sunset:cs_signoff") ? "" : "Requires CS lead or Admin"}>
                <input type="checkbox" checked={csSignoff} disabled={!can("sunset:cs_signoff")} onChange={(e) => setCsSignoff(e.target.checked)} className="accent-black" />CS sign-off on the dossier
              </label>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Gated perm={advancePerm(p)} className="btn-primary" disabled={busy || p.state === "dead_code_pr_opened"}
              onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/advance`, { expected_version: p.version, cs_signoff: csSignoff }))}>
              {advanceLabel(p)}
            </Gated>
            {(p.state === "canary_dark_launch" || p.state === "dead_code_pr_opened") && (
              <Gated perm="sunset:rollback" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/rollback`, { reason: `Rolled back from the dashboard by ${me?.email ?? "user"}` }))}>
                Roll back: flag fully on
              </Gated>
            )}
            <Gated perm="sunset:cancel" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/cancel`, { reason: "Cancelled from dashboard" }))}>Cancel plan</Gated>
            {me && <span className="text-xs text-ink-3">Signed in as {ROLE_LABEL[me.role as Role]}: next step needs {ADVANCE_ROLE[advancePerm(p)]}</span>}
          </div>
        </div>
      )}

      <div className="mb-6 grid gap-4 lg:grid-cols-3">
        <div className="card p-4">
          <div className="label mb-2">Feature flag</div>
          {p.flag_key ? (
            <>
              <div className="flex items-center justify-between gap-2"><code className="text-sm">{p.flag_key}</code><FlagPill flag={p.flag ?? { state: "fully_on", provider: null }} /></div>
              <div className="mt-2 text-xs text-ink-3">
                {p.flag?.provider ? `via ${p.flag.provider}` : "not yet under FeatureLedger control"}
                {p.flag?.state === "zero_usage_off" && ` · still on for ${p.flag.keep_on_for_count ?? 0} account(s) with usage in the last 90 days`}
                {p.flag?.last_error && <div className="mt-1 text-[#8a3413]">Last error: {p.flag.last_error}</div>}
              </div>
              <div className="mt-2 text-xs text-ink-2">Rollbacks: <b className="num">{p.rollback_count ?? 0}</b></div>
            </>
          ) : <div className="text-sm text-ink-3">No flag: a reversible dark launch is impossible. Add <code>flag_key</code> in features.yaml.</div>}
        </div>
        <div className="card p-4">
          <div className="label mb-2">Scream test</div>
          {p.scream_test ? (
            <>
              <div className="num text-xl font-semibold">{Math.min(p.scream_test.quiet_days, p.scream_test.required_days)} / {p.scream_test.required_days} days</div>
              <div className="my-2 h-1.5 rounded-full bg-[#e7e6e2]"><div className="h-1.5 rounded-full bg-ink" style={{ width: `${Math.min(100, 100 * p.scream_test.quiet_days / p.scream_test.required_days)}%` }} /></div>
              <div className="grid grid-cols-3 gap-2 text-xs">
                <Stat label="'on' evaluations" value={p.scream_test.flag_on_evaluations} bad />
                <Stat label="tickets" value={p.scream_test.tickets_since_off} bad />
                <Stat label="'off' evaluations" value={p.scream_test.flag_off_evaluations} />
              </div>
              <div className="mt-2 text-xs text-ink-3">Fully off since {p.scream_test.fully_off_at.slice(0, 10)}. Any 'on' evaluation or ticket blocks the cleanup PR.</div>
            </>
          ) : <div className="text-sm text-ink-3">Starts when the flag goes fully off.</div>}
          {can("flags:simulate") && p.state === "canary_dark_launch" && p.flag_key && (
            <div className="mt-3 flex gap-2 border-t border-rule pt-3">
              <button className="btn text-xs" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/flags/${p.flag_key}/evaluations`, { on_count: 1, off_count: 0 }))}>Sandbox: serve 'on' once</button>
              <button className="btn text-xs" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/flags/${p.flag_key}/evaluations`, { reset: true }))}>Reset</button>
            </div>
          )}
        </div>
        <div className="card p-4">
          <div className="label mb-2">Cleanup pull request</div>
          {p.cleanup_pr?.number ? (
            <>
              <div className="text-sm font-medium">
                {p.cleanup_pr.url ? <a href={p.cleanup_pr.url} target="_blank" rel="noreferrer" className="hover:underline">{p.cleanup_pr.repo}#{p.cleanup_pr.number} ↗</a>
                  : <>{p.cleanup_pr.repo}#{p.cleanup_pr.number} <span className="text-xs text-ink-3">(sandbox)</span></>}
              </div>
              <div className="mt-1 text-xs text-ink-3">branch <code>{p.cleanup_pr.branch}</code> · <b>{p.cleanup_pr.state}</b> · {p.cleanup_pr.files_to_delete.length} files deleted</div>
              <div className="mt-2 text-xs text-ink-2">{p.cleanup_pr.state === "open" ? "Merging it archives the feature automatically (GitHub webhook)." : p.cleanup_pr.state === "merged" ? `Merged ${p.cleanup_pr.merged_at?.slice(0, 10) ?? ""}.` : "Closed."}</div>
              {p.cleanup_pr.state === "open" && p.cleanup_pr.provider === "sandbox" && can("flags:simulate") && (
                <button className="btn mt-3 text-xs" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/sunset-plans/${id}/cleanup-pr/simulate-merge`))}>Sandbox: merge the PR</button>
              )}
            </>
          ) : <div className="text-sm text-ink-3">Opened automatically by FeatureLedger when an admin advances a quiet canary.</div>}
        </div>
      </div>

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
          <SectionTitle title="Dead-code PR" sub={p.cleanup_pr?.number ? "The body FeatureLedger opened the PR with" : "Opened automatically after a quiet scream test"} />
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
                <span><span className="text-ink-3">{l.created_at.slice(0, 16).replace("T", " ")}</span> · {l.action.replace(/_/g, " ")} → {STATE_LABEL[l.to_state] ?? l.to_state}
                  {l.payload.flag_state ? <> · flag <b>{String(l.payload.flag_state).replace(/_/g, " ")}</b></> : null}
                  {l.payload.actor_role ? <span className="text-ink-3"> · {ROLE_LABEL[l.payload.actor_role as Role] ?? String(l.payload.actor_role)}</span> : null}</span>
                <code className="text-ink-3" title={l.hash}>{l.hash.slice(0, 10)}</code>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

const ADVANCE_ROLE: Record<string, string> = {
  "sunset:blast_radius": "PM / Tech lead or Admin", "sunset:cs_signoff": "CS lead or Admin",
  "sunset:approve_canary": "Admin", "sunset:approve_cleanup": "Admin",
};

function advancePerm(p: SunsetPlan): string {
  switch (p.state) {
    case "candidate_for_sunset": return "sunset:blast_radius";
    case "blast_radius_checked": return "sunset:cs_signoff";
    case "customer_outreach": return "sunset:approve_canary";
    case "canary_dark_launch": return p.canary_phase === "zero_usage_off" ? "sunset:approve_canary" : "sunset:approve_cleanup";
    default: return "sunset:approve_cleanup";
  }
}

function advanceLabel(p: SunsetPlan): string {
  switch (p.state) {
    case "candidate_for_sunset": return "Confirm blast radius →";
    case "blast_radius_checked": return "Start customer outreach →";
    case "customer_outreach": return "Approve dark launch (flag off for 0-usage accounts) →";
    case "canary_dark_launch": return p.canary_phase === "zero_usage_off" ? "Turn flag fully off (start scream test) →" : "Approve cleanup PR →";
    case "dead_code_pr_opened": return "Waiting for the PR merge";
    default: return "Advance →";
  }
}

function FlagPill({ flag }: { flag: FlagStatus }) {
  const tone = flag.state === "fully_on" ? "bg-[#e7f5ee] text-[#126b4a]" : flag.state === "fully_off" ? "bg-[#fdf3ee] text-[#8a3413]" : "bg-[#fff6e0] text-[#7a5300]";
  return <span className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${tone}`}>flag {flag.state.replace(/_/g, " ")}</span>;
}

function Stat({ label, value, bad }: { label: string; value: number; bad?: boolean }) {
  return (
    <div>
      <div className={`num text-base font-semibold ${bad && value > 0 ? "text-[#b4471c]" : ""}`}>{value.toLocaleString()}</div>
      <div className="text-ink-3">{label}</div>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="card px-3 py-6 text-center text-sm text-ink-3">{text}</div>;
}
