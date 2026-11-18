"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Gated } from "@/components/Auth";
import { ErrorBox, SectionTitle } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { num } from "@/lib/format";

type Run = {
  id: number; trigger: string; status: "running" | "succeeded" | "partial" | "failed"; started_at: string;
  finished_at: string | null; batches: number; envelopes_seen: number; envelopes_new: number; processed: number;
  dead_lettered: number; error: string | null;
};
type Integration = {
  id: string; kind: string; display_name: string; status: string; sandbox: boolean; sync_interval_minutes: number;
  last_synced_at: string | null; last_error: string | null; live_configured: boolean;
  health: "healthy" | "syncing" | "waiting" | "failing" | "degraded" | "stale" | "not_configured" | "disabled" | "manual";
  cursors: { entity_type: string; cursor: string; updated_at: string }[];
  inbox: { pending: number; processed: number; dead_letters: number };
  recent_runs: Run[];
  manifest?: { status: string; commit_sha: string | null; errors: string[]; received_at: string; source: string } | null;
};
type DeadLetter = { idempotency_key: string; entity_type: string; external_id: string; attempts: number; last_error: string };

const HEALTH: Record<Integration["health"], { label: string; dot: string; text: string }> = {
  healthy: { label: "Healthy", dot: "bg-[#1f9d55]", text: "text-[#17663a]" },
  syncing: { label: "Syncing…", dot: "bg-cow animate-pulse", text: "text-[#1d5fae]" },
  waiting: { label: "Waiting for first sync", dot: "bg-other", text: "text-ink-2" },
  failing: { label: "Failing", dot: "bg-[#c62828]", text: "text-[#9b1c1c]" },
  degraded: { label: "Partially synced", dot: "bg-[#d98a00]", text: "text-[#8a5a00]" },
  stale: { label: "Stale", dot: "bg-[#d98a00]", text: "text-[#8a5a00]" },
  not_configured: { label: "Not configured", dot: "bg-other", text: "text-ink-3" },
  disabled: { label: "Disabled", dot: "bg-other", text: "text-ink-3" },
  manual: { label: "On demand", dot: "bg-other", text: "text-ink-2" },
};
const SOURCE: Record<string, string> = {
  stripe: "Billing & revenue", jira: "Issue tracking", posthog: "Product analytics", zendesk: "Customer support",
  aws_cost_explorer: "Cloud spend", github: "Code, PRs & features.yaml",
};
const ENV_HINT: Record<string, string> = {
  stripe: "STRIPE_API_KEY", jira: "JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN",
  posthog: "POSTHOG_PROJECT_ID, POSTHOG_PERSONAL_API_KEY", zendesk: "ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN",
  aws_cost_explorer: "AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY",
};

function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export default function IntegrationsPage() {
  const [data, setData] = useState<{ integrations: Integration[]; sandbox_mode: boolean } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [dead, setDead] = useState<Record<string, DeadLetter[]>>({});
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(() => apiGet<{ integrations: Integration[]; sandbox_mode: boolean }>("/api/v1/integrations")
    .then((d) => { setData(d); setErr(null); }).catch((e) => setErr(e.message)), []);

  useEffect(() => {
    load();
    timer.current = setInterval(load, 5000);   // live view: syncs run in the background worker
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [load]);

  const trigger = async (i: Integration) => {
    setNotice(null);
    try {
      const r = await apiPost<{ queued: boolean; message: string }>(`/api/v1/integrations/${i.id}/sync`);
      setNotice(`${i.display_name}: ${r.message}`);
      setTimeout(load, 800);
    } catch (e) { setErr((e as Error).message); }
  };
  const loadDead = async (i: Integration) => {
    setOpen(open === i.id ? null : i.id);
    if (open !== i.id) setDead({ ...dead, [i.id]: await apiGet<DeadLetter[]>(`/api/v1/integrations/${i.id}/dead-letters`) });
  };
  const retryDead = async (i: Integration) => {
    const r = await apiPost<{ requeued: number }>(`/api/v1/integrations/${i.id}/dead-letters/retry`);
    setNotice(`${r.requeued} dead letter(s) re-queued for ${i.display_name}`);
    setDead({ ...dead, [i.id]: [] });
    load();
  };

  const list = [...(data?.integrations ?? [])].sort((a, b) => (a.kind === "github" ? 1 : 0) - (b.kind === "github" ? 1 : 0));
  const totals = list.reduce((a, i) => ({ processed: a.processed + i.inbox.processed, pending: a.pending + i.inbox.pending,
    dead: a.dead + i.inbox.dead_letters }), { processed: 0, pending: 0, dead: 0 });

  return (
    <div className="mx-auto max-w-[1280px] space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Integrations & sync health</h1>
          <p className="text-sm text-ink-3">
            Every source syncs every 15 minutes in the background worker. Envelopes are scrubbed, deduplicated and
            committed with their cursor; failures retry 5 times, then park as dead letters.
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs text-ink-2">
          {data?.sandbox_mode && <span className="rounded-full border border-[#c9dcf5] bg-[#eef5fd] px-2.5 py-1 text-[#1d5fae]">Sandbox mode: fixture APIs, no paid accounts</span>}
          <span className="num">{num(totals.processed)} processed · {num(totals.pending)} pending · {num(totals.dead)} dead letters</span>
        </div>
      </header>
      {err && <ErrorBox error={err} />}
      {notice && <div className="card px-4 py-2.5 text-sm text-ink-2">{notice}</div>}

      <div className="grid gap-4 lg:grid-cols-2">
        {list.map((i) => {
          const h = HEALTH[i.health];
          const last = i.recent_runs[0];
          return (
            <section key={i.id} className="card p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-[15px] font-semibold">{i.display_name}</h2>
                    {i.sandbox && <span className="rounded border border-rule px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-ink-3">sandbox</span>}
                  </div>
                  <div className="text-xs text-ink-3">{SOURCE[i.kind] ?? i.kind}</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${h.text}`}>
                    <span className={`h-2 w-2 rounded-full ${h.dot}`} />{h.label}
                  </span>
                  {i.kind !== "github" && <Gated perm="integrations:manage" onClick={() => trigger(i)} disabled={i.health === "syncing"}>Trigger sync</Gated>}
                </div>
              </div>

              <dl className="mt-4 grid grid-cols-4 gap-3 text-xs">
                <Stat k="Last sync" v={ago(i.last_synced_at)} />
                <Stat k="Processed" v={num(i.inbox.processed)} />
                <Stat k="Pending" v={num(i.inbox.pending)} />
                <Stat k="Dead letters" v={num(i.inbox.dead_letters)} bad={i.inbox.dead_letters > 0} />
              </dl>

              {i.health === "not_configured" && (
                <p className="mt-3 rounded-md bg-[#f6f5f2] px-3 py-2 text-xs text-ink-2">
                  Add <code>{ENV_HINT[i.kind]}</code> to <code>.env</code> and restart, or run with <code>SANDBOX_MODE=true</code>.
                </p>
              )}
              {i.last_error && i.health !== "healthy" && (
                <p className="mt-3 rounded-md bg-[#fdf3ee] px-3 py-2 text-xs text-[#8a3413]">{i.last_error}</p>
              )}

              {i.kind === "github" && (
                <div className="mt-3 rounded-md bg-[#f6f5f2] px-3 py-2 text-xs text-ink-2">
                  <b>features.yaml sync on push:</b>{" "}
                  {i.manifest ? <>{i.manifest.status}{i.manifest.commit_sha ? ` @ ${i.manifest.commit_sha.slice(0, 8)}` : ""} ({i.manifest.source.replace("_", " ")})</> : "no push received yet"}
                  {i.manifest?.status === "invalid" && (
                    <ul className="mt-1 list-disc pl-4 text-[#8a3413]">{i.manifest.errors.slice(0, 5).map((e) => <li key={e}>{e}</li>)}</ul>
                  )}
                  <div className="mt-1 text-ink-3">Webhook: <code>/api/v1/webhooks/github/{i.id}</code> (push events)</div>
                </div>
              )}
              {i.cursors.length > 0 && (
                <div className="mt-4">
                  <div className="label mb-1">Cursors</div>
                  <div className="flex flex-wrap gap-1.5">
                    {i.cursors.map((c) => (
                      <span key={c.entity_type} className="rounded-md border border-rule bg-white px-2 py-1 text-[11px]">
                        <span className="text-ink-3">{c.entity_type}</span> <code>{String(c.cursor).slice(0, 19)}</code>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {i.recent_runs.length > 0 && (
                <div className="mt-4">
                  <div className="label mb-1">Recent runs</div>
                  <table className="w-full text-xs">
                    <tbody>
                      {i.recent_runs.slice(0, 4).map((r) => (
                        <tr key={r.id} className="border-t border-rule">
                          <td className="py-1.5 text-ink-3">{ago(r.started_at)}</td>
                          <td className="py-1.5">{r.trigger}</td>
                          <td className={`py-1.5 font-medium ${r.status === "succeeded" ? "text-[#17663a]" : r.status === "running" ? "text-[#1d5fae]" : "text-[#9b1c1c]"}`}>{r.status}</td>
                          <td className="num py-1.5 text-right text-ink-2">{num(r.envelopes_seen)} read · {num(r.envelopes_new)} new · {num(r.processed)} processed</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {last?.error && <p className="mt-1 truncate text-[11px] text-[#9b1c1c]" title={last.error}>{last.error}</p>}
                </div>
              )}

              {i.inbox.dead_letters > 0 && (
                <div className="mt-3">
                  <button className="text-xs font-medium text-[#9b1c1c] hover:underline" onClick={() => loadDead(i)}>
                    {open === i.id ? "Hide" : "Show"} dead letters
                  </button>
                  {open === i.id && (
                    <div className="mt-2 space-y-1">
                      {(dead[i.id] ?? []).map((d) => (
                        <div key={d.idempotency_key} className="rounded-md bg-[#fdf3ee] px-2 py-1 text-[11px]">
                          <b>{d.entity_type}</b> {d.external_id} · {d.attempts} attempts · {d.last_error}
                        </div>
                      ))}
                      <Gated perm="integrations:manage" className="btn mt-1" onClick={() => retryDead(i)}>Retry all</Gated>
                    </div>
                  )}
                </div>
              )}
            </section>
          );
        })}
      </div>

      <section>
        <SectionTitle title="How a sync works" sub="Identical for live APIs and the sandbox" />
        <ol className="card grid gap-4 p-4 text-xs text-ink-2 md:grid-cols-5">
          {[
            ["Fetch", "Backfill on first run, then incremental from the stored cursor; token-bucket rate limits and backoff with jitter."],
            ["Scrub", "Allowlist per source: no assignees, reporters, emails, contact names or ticket threads ever stored."],
            ["Commit", "Envelopes + cursor in one transaction. The idempotency key makes replays free."],
            ["Process", "Stitch identities (Stripe id, PostHog group, Zendesk org, email domain), rebuild months, isolate failures."],
            ["Recompute", "The Feature P&L is recomputed when anything new landed."],
          ].map(([t, d], n) => (
            <li key={t}><div className="mb-1 font-semibold text-ink"><span className="num text-ink-3">{n + 1}</span> {t}</div>{d}</li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function Stat({ k, v, bad }: { k: string; v: string; bad?: boolean }) {
  return (
    <div>
      <dt className="text-ink-3">{k}</dt>
      <dd className={`num mt-0.5 text-sm font-medium ${bad ? "text-[#9b1c1c]" : ""}`}>{v}</dd>
    </div>
  );
}
