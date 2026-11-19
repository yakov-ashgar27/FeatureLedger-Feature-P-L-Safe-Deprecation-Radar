"use client";

import { useCallback, useEffect, useState } from "react";
import { NEEDS, ROLE_LABEL, useCan, useMe } from "@/components/Auth";
import { ErrorBox, SectionTitle } from "@/components/ui";
import { apiGet, apiPatch, apiPost, apiPut } from "@/lib/api";
import type { Role } from "@/lib/types";

type Settings = Record<string, number | string>;
type User = { id: string; email: string; name: string | null; role: string; disabled: boolean; last_login_at: string | null; sso: boolean };
type Matrix = { roles: Role[]; matrix: ({ permission: string; description: string } & Record<Role, boolean>)[]; you: Role };
type Flags = { provider: string; flags: { feature_key: string; feature_name: string; flag_key: string; state: string; provider: string | null;
  keep_on_for_count: number; on_7d: number; off_7d: number; feature_status: string }[] };

const FIELDS: { key: string; label: string; step: number; hint: string }[] = [
  { key: "eng_hourly_rate_usd", label: "Engineering rate ($/h)", step: 1, hint: "Loaded cost per engineering hour" },
  { key: "support_hourly_rate_usd", label: "Support rate ($/h)", step: 1, hint: "Loaded cost per support hour" },
  { key: "hours_per_engineer_month", label: "Hours / engineer / month", step: 1, hint: "40 to 220" },
  { key: "allocatable_fraction", label: "Allocatable fraction", step: 0.01, hint: "Share of capacity spent on feature work" },
  { key: "calibration_clamp_max", label: "Calibration clamp", step: 0.1, hint: "Max effort multiplier per team" },
  { key: "critical_concentration", label: "Critical concentration", step: 0.01, hint: "ARR share that marks a niche anchor" },
  { key: "anchor_min_arr_usd", label: "Niche anchor min ARR ($)", step: 1000, hint: "Below this, no anchor protection" },
  { key: "mapping_auto_confirm", label: "Auto-confirm threshold", step: 0.01, hint: "Tier 2/3 confidence to apply without triage" },
  { key: "min_team_size_for_display", label: "Min team size shown", step: 1, hint: "Smaller teams roll up (privacy)" },
];

export default function SettingsPage() {
  const can = useCan();
  const me = useMe();
  const [s, setS] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings>({});
  const [users, setUsers] = useState<User[] | null>(null);
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [flags, setFlags] = useState<Flags | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiGet<Settings>("/api/v1/settings").then((x) => { setS(x); setDraft({}); }).catch((e) => setErr(e.message));
    apiGet<Matrix>("/api/v1/auth/rbac").then(setMatrix).catch(() => undefined);
    apiGet<Flags>("/api/v1/flags").then(setFlags).catch(() => undefined);
    if (can("users:manage")) apiGet<User[]>("/api/v1/users").then(setUsers).catch(() => undefined);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me?.role]);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true); setErr(null); setNote(null);
    try {
      const body = Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, Number(v)]));
      const r = await apiPut<{ changed: string[] }>("/api/v1/settings", body);
      setNote(`Saved ${r.changed.join(", ")}; the P&L was recomputed.`);
      load();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  const patchUser = async (u: User, body: Partial<User>) => {
    setErr(null);
    try { await apiPatch(`/api/v1/users/${u.id}`, body); load(); } catch (e) { setErr((e as Error).message); }
  };
  const revoke = async (u: User) => {
    setErr(null);
    try { await apiPost(`/api/v1/users/${u.id}/revoke-sessions`); setNote(`${u.email} was signed out on every device.`); }
    catch (e) { setErr((e as Error).message); }
  };
  const editable = can("settings:write");

  return (
    <div className="mx-auto max-w-[1180px] space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Settings &amp; access</h1>
        <p className="text-sm text-ink-3">Rates and thresholds that drive the P&amp;L, who can do what, and the flags FeatureLedger controls.</p>
      </header>
      {err && <ErrorBox error={err} />}
      {note && <div className="card px-4 py-3 text-sm text-ink-2">{note}</div>}

      <section>
        <SectionTitle title="Organization settings" sub={editable ? "Changes recompute the P&L immediately" : `Read-only: changing these requires ${NEEDS["settings:write"]}`}
          right={editable ? <button className="btn-primary" disabled={busy || !Object.keys(draft).length} onClick={save}>Save changes</button> : undefined} />
        <div className="card grid gap-x-6 gap-y-4 p-5 md:grid-cols-3">
          {s && FIELDS.map((f) => (
            <label key={f.key} className="block">
              <span className="label">{f.label}</span>
              <input type="number" step={f.step} disabled={!editable} value={String(draft[f.key] ?? s[f.key] ?? "")}
                onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                className="mt-1 w-full rounded-md border border-rule bg-white px-2 py-1.5 text-sm disabled:bg-[#f6f5f2]" name={f.key} />
              <span className="text-[11px] text-ink-3">{f.hint}</span>
            </label>
          ))}
        </div>
      </section>

      {users && (
        <section>
          <SectionTitle title="Users & roles" sub="SSO users are created as Viewer on first sign-in unless your IdP group maps to a role (OIDC_ROLE_MAP)" />
          <div className="card divide-y divide-rule">
            {users.map((u) => (
              <div key={u.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5 text-sm">
                <div className="min-w-0">
                  <div className="truncate font-medium">{u.name ?? u.email}{u.sso && <span className="ml-2 text-[11px] text-ink-3">SSO</span>}</div>
                  <div className="truncate text-xs text-ink-3">{u.email} · last sign-in {u.last_login_at ? u.last_login_at.slice(0, 10) : "never"}</div>
                </div>
                <div className="flex items-center gap-2">
                  <select value={u.role} disabled={u.id === me?.id} onChange={(e) => patchUser(u, { role: e.target.value })}
                    className="rounded-md border border-rule bg-white px-2 py-1 text-sm">
                    {(["viewer", "pm_tech_lead", "cs_lead", "admin"] as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
                    {!(u.role in ROLE_LABEL) && <option value={u.role}>{u.role} (legacy)</option>}
                  </select>
                  <button className="btn text-xs" disabled={u.id === me?.id} onClick={() => patchUser(u, { disabled: !u.disabled })}>{u.disabled ? "Enable" : "Disable"}</button>
                  <button className="btn text-xs" disabled={u.id === me?.id || u.disabled} title="Ends every session of this user (lost laptop, leaked token)"
                    onClick={() => revoke(u)}>Sign out everywhere</button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {matrix && (
        <section>
          <SectionTitle title="Role permissions" sub={`Enforced by the API on every request. You are ${ROLE_LABEL[matrix.you]}.`} />
          <div className="card overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-rule text-left">
                <th className="px-4 py-2 font-medium">Permission</th>
                {matrix.roles.map((r) => <th key={r} className={`px-3 py-2 text-center font-medium ${r === matrix.you ? "bg-[#f3f2ee]" : ""}`}>{ROLE_LABEL[r]}</th>)}
              </tr></thead>
              <tbody>
                {matrix.matrix.map((m) => (
                  <tr key={m.permission} className="border-b border-rule last:border-0">
                    <td className="px-4 py-2"><code className="text-xs">{m.permission}</code><div className="text-xs text-ink-3">{m.description}</div></td>
                    {matrix.roles.map((r) => <td key={r} className={`px-3 py-2 text-center ${r === matrix.you ? "bg-[#f3f2ee]" : ""}`}>{m[r] ? "✓" : <span className="text-ink-3">–</span>}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {flags && (
        <section>
          <SectionTitle title="Feature flags" sub={`Provider: ${flags.provider}. Flags move only through Safe Sunset steps; rollback sets them fully on.`} />
          <div className="card divide-y divide-rule">
            {flags.flags.map((f) => (
              <div key={f.flag_key} className="flex items-center justify-between gap-3 px-4 py-2 text-sm">
                <div><code>{f.flag_key}</code> <span className="text-xs text-ink-3">· {f.feature_name}{f.feature_status === "archived" ? " (archived)" : ""}</span></div>
                <div className="num text-xs text-ink-2">{f.state.replace(/_/g, " ")}{f.provider ? ` · ${f.provider}` : ""} · 7 d: {f.on_7d.toLocaleString()} on / {f.off_7d.toLocaleString()} off</div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
