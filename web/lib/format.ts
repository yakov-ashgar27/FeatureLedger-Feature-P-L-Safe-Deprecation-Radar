import type { FeatureClass } from "./types";

export const usd = (v: number | null | undefined, digits = 0) =>
  v == null ? "—" : v.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: digits });

export const usdShort = (v: number | null | undefined) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  const sign = v < 0 ? "−" : "";
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M`;
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k`;
  return `${sign}$${a.toFixed(0)}`;
};

export const pct = (v: number | null | undefined, digits = 1) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);
export const num = (v: number | null | undefined, digits = 0) =>
  v == null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits });

export const CLASS_META: Record<FeatureClass, { label: string; dot: string; hint: string }> = {
  cash_cow: { label: "Cash Cow", dot: "bg-cow", hint: "High value, manageable cost" },
  zombie: { label: "Zombie", dot: "bg-zombie", hint: "Low usage, negative ROI, high bug tax: sunset candidate" },
  niche_anchor: { label: "Niche Anchor", dot: "bg-anchor", hint: "Low adoption but a high-ARR account depends on it" },
  growth_bet: { label: "Growth Bet", dot: "bg-other", hint: "Investing ahead of value, adoption rising" },
  watch: { label: "Watch", dot: "bg-other", hint: "No rule triggered" },
  mandated: { label: "Mandated", dot: "bg-other", hint: "Regulatory: exempt from sunset" },
  platform: { label: "Platform", dot: "bg-other", hint: "Shared overhead" },
};

export const STATE_LABEL: Record<string, string> = {
  candidate_for_sunset: "Candidate",
  blast_radius_checked: "Blast radius checked",
  customer_outreach: "Customer outreach",
  canary_dark_launch: "Canary dark launch",
  dead_code_pr_opened: "Dead-code PR open",
  archived: "Archived",
  cancelled: "Cancelled",
};
