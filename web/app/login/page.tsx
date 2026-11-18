"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { ROLE_LABEL } from "@/components/Auth";
import { apiGet, apiPost } from "@/lib/api";
import type { AuthConfig, Role } from "@/lib/types";

const ROLE_HINT: Record<Role, string> = {
  viewer: "Read-only: P&L, tear-sheets, simulator",
  pm_tech_lead: "Triage mappings, start sunsets, acknowledge blast radius",
  cs_lead: "Sign off the impact dossier, mark accounts contacted",
  admin: "Integrations, settings, approve canary + cleanup PR",
};

export default function LoginPage() {
  return <Suspense><Login /></Suspense>;
}

function Login() {
  const sp = useSearchParams();
  const next = sp?.get("next") || "/";
  const [cfg, setCfg] = useState<AuthConfig | null>(null);
  const [err, setErr] = useState<string | null>(sp?.get("error") ?? null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => { apiGet<AuthConfig>("/api/v1/auth/config").then(setCfg).catch((e) => setErr(e.message)); }, []);

  const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";
  const dev = async (role: Role) => {
    setBusy(role); setErr(null);
    try {
      await apiPost("/api/v1/auth/dev-login", { role });
      window.location.href = safeNext;
    } catch (e) { setErr((e as Error).message); setBusy(null); }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#f6f5f2] px-4">
      <div className="card w-full max-w-md p-7">
        <div className="mb-6">
          <div className="text-lg font-semibold tracking-tight">FeatureLedger</div>
          <div className="text-sm text-ink-3">Feature-level P&amp;L and Safe Deprecation Radar</div>
        </div>
        {err && <div className="mb-4 rounded-md border border-[#f1c3b0] bg-[#fdf3ee] px-3 py-2 text-sm text-[#8a3413]">{err}</div>}
        {cfg?.oidc.enabled && (
          <a className="btn-primary block w-full text-center" href={`/api/v1/auth/oidc/login?next=${encodeURIComponent(safeNext)}`}>
            Sign in with {cfg.oidc.label}
          </a>
        )}
        {cfg?.dev_login && (
          <div className={cfg.oidc.enabled ? "mt-6 border-t border-rule pt-5" : ""}>
            <div className="label mb-1">Sandbox sign-in</div>
            <p className="mb-3 text-xs text-ink-3">SANDBOX_MODE is on: sign in as a seeded Northwind user of each role. Disabled automatically in production.</p>
            <div className="space-y-2">
              {(["admin", "pm_tech_lead", "cs_lead", "viewer"] as Role[]).map((r) => (
                <button key={r} className="card flex w-full items-center justify-between px-3 py-2.5 text-left hover:border-ink-3"
                  disabled={!!busy} onClick={() => dev(r)} data-role={r}>
                  <span>
                    <span className="block text-sm font-medium">{ROLE_LABEL[r]}</span>
                    <span className="block text-xs text-ink-3">{ROLE_HINT[r]}</span>
                  </span>
                  <span className="text-xs text-ink-3">{busy === r ? "…" : "→"}</span>
                </button>
              ))}
            </div>
          </div>
        )}
        {cfg && !cfg.oidc.enabled && !cfg.dev_login && (
          <p className="text-sm text-ink-2">No sign-in method is configured. Set <code>OIDC_PROVIDER</code>, <code>OIDC_ISSUER</code> and <code>OIDC_CLIENT_ID</code> (see <code>.env.example</code>).</p>
        )}
      </div>
    </div>
  );
}
