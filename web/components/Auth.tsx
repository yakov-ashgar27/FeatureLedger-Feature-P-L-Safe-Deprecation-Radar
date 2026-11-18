"use client";

import { createContext, useContext } from "react";
import type { Me, Role } from "@/lib/types";

const MeContext = createContext<Me | null>(null);

export function MeProvider({ me, children }: { me: Me | null; children: React.ReactNode }) {
  return <MeContext.Provider value={me}>{children}</MeContext.Provider>;
}

export function useMe(): Me | null {
  return useContext(MeContext);
}

/** UI gate only: the API enforces the same matrix on every request. */
export function useCan(): (permission: string) => boolean {
  const me = useMe();
  return (permission) => !!me?.permissions.includes(permission);
}

export const ROLE_LABEL: Record<Role, string> = {
  viewer: "Viewer", pm_tech_lead: "PM / Tech lead", cs_lead: "CS lead", admin: "Admin",
};

// who to ask, shown next to a disabled control
export const NEEDS: Record<string, string> = {
  "triage:decide": "PM / Tech lead or Admin",
  "graph:run": "PM / Tech lead or Admin",
  "pnl:recompute": "PM / Tech lead or Admin",
  "sunset:create": "PM / Tech lead or Admin",
  "sunset:blast_radius": "PM / Tech lead or Admin",
  "sunset:dossier": "PM / Tech lead, CS lead or Admin",
  "sunset:cs_signoff": "CS lead or Admin",
  "sunset:approve_canary": "Admin",
  "sunset:approve_cleanup": "Admin",
  "sunset:rollback": "PM / Tech lead, CS lead or Admin",
  "sunset:cancel": "PM / Tech lead or Admin",
  "integrations:manage": "Admin",
  "settings:write": "Admin",
  "users:manage": "Admin",
  "flags:simulate": "Admin (sandbox)",
};

/** A button that stays visible but disabled (with the reason) when the role lacks the permission. */
export function Gated({ perm, className = "btn", disabled, onClick, children, title }: {
  perm: string; className?: string; disabled?: boolean; onClick?: () => void; children: React.ReactNode; title?: string;
}) {
  const can = useCan()(perm);
  return (
    <button className={className} disabled={disabled || !can} onClick={onClick}
      title={can ? title : `Requires ${NEEDS[perm] ?? perm}`} data-perm={perm}>
      {children}
    </button>
  );
}
