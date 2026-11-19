"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ROLE_LABEL, useMe } from "@/components/Auth";
import { apiPost } from "@/lib/api";

const LINKS = [
  { href: "/", label: "Feature P&L", sub: "Ledger & quadrant" },
  { href: "/simulator", label: "What-if simulator", sub: "Reclaimable capacity" },
  { href: "/sunset", label: "Safe Sunset", sub: "Deprecation pipeline" },
  { href: "/mappings", label: "Feature Graph", sub: "Mapping triage" },
  { href: "/integrations", label: "Integrations", sub: "Sync health" },
  { href: "/zombie-guard", label: "Zombie-Guard", sub: "PR bot preview" },
  { href: "/reports", label: "Executive brief", sub: "Digest, Slack, PDF" },
  { href: "/settings", label: "Settings & access", sub: "Rates, users, roles" },
];

export function Nav() {
  const path = usePathname() ?? "/";
  const me = useMe();
  const signOut = async () => {
    try { await apiPost("/api/v1/auth/logout"); } finally { window.location.href = "/login"; }
  };
  return (
    <nav className="sticky top-0 flex h-screen print:hidden w-60 shrink-0 flex-col border-r border-rule bg-surface px-4 py-6">
      <Link href="/" className="mb-8 block px-2">
        <div className="text-[15px] font-semibold tracking-tight">FeatureLedger</div>
        <div className="text-xs text-ink-3">Northwind Cloud</div>
      </Link>
      <ul className="space-y-1">
        {LINKS.map((l) => {
          const active = l.href === "/" ? path === "/" || path.startsWith("/features") : path.startsWith(l.href);
          return (
            <li key={l.href}>
              <Link
                href={l.href}
                className={`block rounded-md px-2 py-2 ${active ? "bg-[#efeee9] text-ink" : "text-ink-2 hover:bg-[#f3f2ee]"}`}
              >
                <div className="text-sm font-medium">{l.label}</div>
                <div className="text-xs text-ink-3">{l.sub}</div>
              </Link>
            </li>
          );
        })}
      </ul>
      {me && (
        <div className="mt-auto mb-4 rounded-md border border-rule px-2.5 py-2" data-testid="whoami">
          <div className="truncate text-xs font-medium">{me.name ?? me.email}</div>
          <div className="truncate text-[11px] text-ink-3">{me.email}</div>
          <div className="mt-1.5 flex items-center justify-between">
            <span className="rounded-full bg-[#efeee9] px-2 py-0.5 text-[11px] text-ink-2" data-role={me.role}>{ROLE_LABEL[me.role]}</span>
            <button onClick={signOut} className="text-[11px] text-ink-3 hover:text-ink">Sign out</button>
          </div>
        </div>
      )}
      <div className={`${me ? "" : "mt-auto "}px-2 text-[11px] leading-relaxed text-ink-3`}>
        Engineering cost is aggregated by feature and team only. No individual developer metrics are stored.
      </div>
    </nav>
  );
}
