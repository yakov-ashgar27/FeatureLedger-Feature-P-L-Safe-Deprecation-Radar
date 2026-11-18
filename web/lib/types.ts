export type FeatureClass =
  | "cash_cow" | "growth_bet" | "niche_anchor" | "zombie" | "watch" | "mandated" | "platform";

export type AttributionModel = "usage_weighted" | "tier_gated" | "critical_dependency";
export type CostView = "contribution" | "fully_loaded";

export interface LedgerRow {
  feature_id: string;
  key: string;
  name: string;
  team: string | null;
  status: string;
  sunset_state: string | null;
  tier_gate: string | null;
  flag_key: string | null;
  is_shared_overhead: boolean;
  is_regulatory: boolean;
  classification: FeatureClass;
  classification_reason: string;
  eng_hours: number;
  c_eng: number;
  c_support: number;
  c_infra: number;
  c_overhead_alloc: number;
  c_total: number;
  value: number;
  net_roi: number;
  efficiency: number;
  adoption: number;
  active_accounts: number;
  eligible_accounts: number;
  anchor_accounts: number;
  revenue_at_risk_arr: number;
  bug_tax_ratio: number;
  tickets: number;
}

export interface Ledger {
  period: string;
  window: number;
  model: AttributionModel;
  view: CostView;
  summary: {
    features: number;
    monthly_cost: number;
    monthly_value: number | null;
    platform_overhead: number;
    zombie_count: number;
    zombie_tax_month: number;
    zombie_eng_hours_month: number;
    untagged_infra_month: number;
    eng_capacity_hours: number;
    unmapped_eng_hours: number;
    unattributed_eng_hours: number;
  };
  features: LedgerRow[];
}

export interface PnlMonth {
  period_month: string;
  eng_hours: number;
  bug_hours: number;
  c_eng: number;
  c_support: number;
  c_infra: number;
  c_total: number;
  c_overhead_alloc: number;
  v_usage: number;
  v_tier: number;
  revenue_at_risk_arr: number;
  tickets: number;
  active_accounts: number;
  eligible_accounts: number;
  anchor_accounts: number;
  bug_tax_ratio: number;
  classification: FeatureClass;
  classification_reason: string;
}

export interface TearSheet {
  feature: {
    id: string; key: string; name: string; description: string | null; team: string | null;
    status: string; flag_key: string | null; tier_gate: string | null; is_regulatory: boolean;
    is_shared_overhead: boolean; launched_at: string | null; metadata: { replaced_by?: string };
  };
  history: PnlMonth[];
  accounts: {
    id: string; name: string; plan_tier: string; health: string; arr_usd: number; renewal_date: string | null;
    feature_active_days: number; account_active_days: number; dependency_score: number; churn_risk: number;
    attributed_mrr: number;
  }[];
  top_bug_files: { path: string; bug_prs: number; churn: number }[];
  assets: { asset_type: string; identifier: string; source: string; confidence: number; status: string }[];
  sunset_plan: { id: string; state: string } | null;
}

export interface SimulationResult {
  features: { feature_key: string; reclaimed_hours_month: number; avoidable_cost_month: number; attributed_mrr: number }[];
  reclaimed_hours_month: number;
  fte_reclaimed: number;
  avoidable_cost_month: number;
  avoidable_cost_year: number;
  accounts_affected: number;
  arr_touched: number;
  expected_arr_loss: number;
  one_off_migration_cost: number;
  net_annual_impact: number;
  migration_success_assumed: number;
  accounts: { account_id: string; name: string; arr_usd: number; churn_risk: number; features: string[] }[];
  period: string;
}

export type SunsetState =
  | "candidate_for_sunset" | "blast_radius_checked" | "customer_outreach" | "canary_dark_launch"
  | "dead_code_pr_opened" | "archived" | "cancelled";

export interface SunsetPlan {
  id: string;
  feature_id: string;
  feature_key: string;
  feature_name: string;
  flag_key: string | null;
  state: SunsetState;
  canary_phase: "zero_usage_off" | "fully_off" | null;
  scream_test_days: number;
  reason: string;
  version: number;
  state_entered_at: string;
  days_in_state?: number;
  metrics?: { eng_hours: number; c_total: number; revenue_at_risk_arr: number; active_accounts: number } | null;
  has_blast_radius?: boolean;
  has_impact_dossier?: boolean;
  has_cleanup_pr?: boolean;
  blast_radius?: {
    inbound_count: number; risk: string; by_owner_feature: Record<string, number>;
    inbound_references: { from_path: string; owner_feature: string; imports: string[]; acknowledged: boolean }[];
  } | null;
  impact_dossier?: {
    accounts_affected: number; arr_touched_usd: number; arr_at_risk_usd: number; replacement: string | null;
    proposed_sunset_date: string;
    accounts: { account_id: string; name: string; arr_usd: number; churn_risk: number; plan_tier: string;
      feature_active_days_90d: number; acknowledged: boolean; email_draft: string; csm_email: string | null }[];
  } | null;
  canary?: { phase: string | null; fully_off_at?: string; started_at?: string; rollbacks?: number; keep_on_for_count?: number;
    provider?: string; alerted?: { on: number; tickets: number } } | null;
  rollback_count?: number;
  cleanup_pr?: {
    title: string; branch: string; body_markdown: string; files_to_delete: string[]; files_to_edit: string[];
    provider?: "sandbox" | "github"; repo?: string; number?: number; url?: string | null; state?: "open" | "merged" | "closed";
    opened_at?: string; merged_at?: string; merge_sha?: string; routes_to_remove?: string[];
  } | null;
  audit_log?: AuditEntry[];
  flag?: FlagStatus | null;
  pull_request?: { provider: string; repo: string; number: number; url: string | null; branch: string; state: string } | null;
  pull_requests?: { provider: string; repo: string; number: number; url: string | null; branch: string; state: string;
    created_at: string; merged_at: string | null; merge_sha: string | null }[];
  scream_test?: ScreamTest | null;
}

export interface AuditEntry {
  id: number; from_state: string | null; to_state: string; action: string; created_at: string; hash: string;
  prev_hash?: string | null;
  payload: Record<string, unknown> & { actor?: string; actor_role?: string; flag_state?: string };
}

export interface FlagStatus {
  state: "fully_on" | "zero_usage_off" | "fully_off";
  provider: string | null;
  keep_on_for_count?: number;
  updated_at?: string;
  last_error?: string | null;
}

export interface ScreamTest {
  fully_off_at: string; quiet_days: number; required_days: number; flag_on_evaluations: number;
  flag_off_evaluations: number; tickets_since_off: number;
}

export type Role = "viewer" | "pm_tech_lead" | "cs_lead" | "admin";

export interface Me {
  id: string | null; email: string; name: string | null; role: Role; kind: "user" | "service";
  org: { id: string; slug: string }; permissions: string[];
}

export interface AuthConfig {
  sandbox_mode: boolean; dev_login: boolean; roles: Role[];
  oidc: { enabled: boolean; provider?: string; label?: string };
}

export interface TriageItem {
  id: string;
  subject_type: "work_item" | "support_ticket" | "code_asset";
  subject_label: string;
  confidence: number;
  source: string;
  rationale: string;
  suggested_feature_key: string | null;
  suggested_feature_name: string | null;
}
