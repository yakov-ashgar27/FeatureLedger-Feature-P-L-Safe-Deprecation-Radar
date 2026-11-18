"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Feature P&L", sub: "Ledger & quadrant" },
  { href: "/simulator", label: "What-if simulator", sub: "Reclaimable capacity" },
  { href: "/sunset", label: "Safe Sunset", sub: "Deprecation pipeline" },
  { href: "/mappings", label: "Feature Graph", sub: "Mapping triage" },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r border-rule bg-surface px-4 py-6">
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
      <div className="mt-auto px-2 text-[11px] leading-relaxed text-ink-3">
        Engineering cost is aggregated by feature and team only. No individual developer metrics are stored.
      </div>
    </nav>
  );
}
