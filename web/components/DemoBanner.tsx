"use client";

import { useState } from "react";
import { ROLE_LABEL, useMe } from "@/components/Auth";
import { apiPost } from "@/lib/api";
import type { Role } from "@/lib/types";

const TRY: Record<Role, string> = {
  viewer: "browse the P&L, the Token-Bleed Radar and the simulator",
  pm_tech_lead: "start a sunset on Legacy PDF Exporter, preview Zombie-Guard, run the simulator",
  cs_lead: "sign off the customer dossier on a plan in outreach",
  admin: "Trigger Sync on Integrations, approve the canary, open and merge the cleanup PR, send the digest",
};

/** Live demo only: who you are, one-click role switch, and a reset back to the seed state. */
export function DemoBanner() {
  const me = useMe();
  const [busy, setBusy] = useState<string | null>(null);
  if (!me?.demo) return null;
  const switchTo = async (role: Role) => {
    setBusy("role");
    try { await apiPost("/api/v1/demo/switch-role", { role }); window.location.reload(); } finally { setBusy(null); }
  };
  const reset = async () => {
    if (!window.confirm("Reset the demo to its starting data? Every change made in the demo is discarded.")) return;
    setBusy("reset");
    try { await apiPost("/api/v1/demo/reset"); window.location.href = "/"; } finally { setBusy(null); }
  };
  return (
    <div className="sticky top-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-[#e9d9a8] bg-[#fff8e3] px-6 py-2 text-sm print:hidden" data-testid="demo-banner">
      <span className="rounded-full bg-[#1d1d1b] px-2 py-0.5 text-[11px] font-medium text-white">LIVE DEMO</span>
      <span className="text-ink-2">Acme Cloud sample data · isolated from real tenants · nothing leaves this sandbox</span>
      <label className="ml-auto flex items-center gap-2">
        <span className="text-ink-3">You are</span>
        <select value={me.role} disabled={!!busy} onChange={(e) => switchTo(e.target.value as Role)} aria-label="Demo role"
          className="rounded-md border border-rule bg-white px-2 py-1 text-sm">
          {(["viewer", "pm_tech_lead", "cs_lead", "admin"] as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
        </select>
      </label>
      <button className="btn text-xs" disabled={!!busy} onClick={reset}>{busy === "reset" ? "Resetting…" : "Reset demo"}</button>
      <span className="basis-full text-xs text-ink-3">Try: {TRY[me.role as Role]}.</span>
    </div>
  );
}
