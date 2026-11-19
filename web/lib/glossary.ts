// Plain-language definitions of every number and label in FeatureLedger. One source for the column
// tooltips, the badges and the /glossary page, so the words never drift apart.

export interface Term { term: string; short: string; long: string; group: "Money" | "Usage" | "Classes" | "Trust" | "Sunset" }

export const GLOSSARY: Record<string, Term> = {
  c_eng: { group: "Money", term: "Engineering cost", short: "Developer time spent on this feature this month, in dollars.",
    long: "Hours of merged work mapped to the feature (estimated from pull-request size unless the team records hours), times the loaded hourly rate in Settings. Bug fixes count too: that is the bug tax." },
  c_support: { group: "Money", term: "Support cost", short: "Time support agents spent on this feature's tickets, in dollars.",
    long: "Tickets mapped to the feature, times the time spent on each (measured when your helpdesk records it, otherwise the default minutes per ticket in Settings), times the support hourly rate." },
  c_infra: { group: "Money", term: "Cloud infra", short: "The feature's share of the cloud bill.",
    long: "Spend tagged with the feature directly, plus a share of untagged spend split by usage. The more of it is tagged, the more precise it is." },
  c_ai_tokens: { group: "Money", term: "AI tokens", short: "What the feature spends on LLM / AI model calls.",
    long: "Model-provider spend (OpenAI, Anthropic, Bedrock...) attributed to the feature. Watched separately because it grows with every use." },
  c_total: { group: "Money", term: "Total cost", short: "Engineering + support + cloud infra + AI tokens.", long: "The full monthly cost of keeping the feature alive." },
  value: { group: "Money", term: "Value", short: "The share of subscription revenue this feature earns, per month.",
    long: "Usage-weighted by default: each paying account's monthly revenue is split across the features it actually uses, in proportion to how much it uses them. The tier-gated model instead credits a feature with the revenue of the plan tier it is sold in." },
  net_roi: { group: "Money", term: "Net / month", short: "Value minus total cost. Negative means it costs more than it earns.",
    long: "The feature's monthly profit or loss under the selected value model." },
  efficiency: { group: "Money", term: "Efficiency", short: "Value earned per dollar of cost (2.0× = two dollars back per dollar spent).",
    long: "Value divided by total cost. Cash Cows clear the efficiency bar set in Settings." },
  revenue_at_risk_arr: { group: "Money", term: "ARR at risk", short: "Yearly revenue from accounts that might churn if this feature disappeared.",
    long: "For each paying account: its ARR times its churn risk, which rises with how much of its activity is concentrated in this feature, how close its renewal is, and its health score." },
  gross_margin: { group: "Money", term: "Gross margin", short: "Value left after cloud and AI costs, as a share of value.",
    long: "(Value - cloud infra - AI tokens) / value. Engineering and support are excluded on purpose: this is the cost of serving each use." },
  margin_bleed: { group: "Money", term: "Margin bleed", short: "An AI feature whose token costs eat most of the revenue it earns.",
    long: "Flagged when gross margin falls below the floor in Settings: usage growth makes it lose more, not less." },

  adoption: { group: "Usage", term: "Adoption", short: "Share of eligible paying accounts that used the feature this month.",
    long: "Active accounts divided by paying accounts whose plan includes the feature (an Enterprise-only feature is measured against Enterprise accounts only)." },
  bug_tax_ratio: { group: "Usage", term: "Bug tax", short: "Share of the feature's engineering time spent fixing bugs instead of building.",
    long: "Bug-fix hours divided by all engineering hours on the feature over the classification window." },

  cash_cow: { group: "Classes", term: "Cash Cow", short: "Earns well above what it costs.", long: "Efficiency at or above the bar in Settings." },
  zombie: { group: "Classes", term: "Zombie", short: "Little used, loses money, and costs real effort: a candidate to retire.",
    long: "Low adoption, negative net over the window, usage not growing, and either a high bug tax or a high monthly cost. A Zombie is a question, not a verdict: check ARR at risk and the confidence badge first." },
  niche_anchor: { group: "Classes", term: "Niche Anchor", short: "Few use it, but a high-value account depends on it. Do not remove blindly.",
    long: "Low adoption, yet at least one account with significant ARR relies heavily on it." },
  growth_bet: { group: "Classes", term: "Growth Bet", short: "New or growing fast; still costs more than it earns.", long: "Young (or adoption rising quickly) and not yet paying for itself: judged on trajectory, not on this month." },
  watch: { group: "Classes", term: "Watch", short: "No rule fired. Nothing to do now.", long: "Neither clearly profitable nor a retirement candidate." },
  mandated: { group: "Classes", term: "Mandated", short: "Required for regulatory reasons; never offered for retirement.", long: "Marked regulatory in features.yaml." },
  platform: { group: "Classes", term: "Platform", short: "Shared infrastructure every feature relies on (auth, billing...).",
    long: "Marked shared_overhead in features.yaml. It has costs but no revenue of its own by design." },

  confidence: { group: "Trust", term: "Confidence", short: "How much of this number rests on measured, declared data rather than estimates.",
    long: "High (●●●): mostly declared mappings and measured data, and the verdict does not change between value models. Medium (●●○): a fair share is inferred or estimated. Low (●○○): mostly estimated, no revenue data, or the verdict flips depending on the value model. Open a feature and look at 'Why this number?' for the details." },
  inferred: { group: "Trust", term: "Inferred mapping", short: "Work or tickets linked to a feature by FeatureLedger, not declared by your team.",
    long: "Code paths, labels and text similarity link items to features. Confident matches are applied automatically; the rest wait in Feature Graph triage for a human decision." },
  attribution_model: { group: "Trust", term: "Value model", short: "How subscription revenue is divided among features.",
    long: "Usage-weighted (default) follows what customers actually use. Tier-gated credits the plan tier a feature is sold in. When the two disagree on whether a feature makes money, its confidence is low." },

  sunset: { group: "Sunset", term: "Safe Sunset", short: "The guarded, reversible process for retiring a feature.",
    long: "Candidate → blast radius checked → customer outreach → canary (dark launch) → cleanup PR → archived. Each step has guards; the flag can be switched back on at any point until the code is deleted." },
  blast_radius: { group: "Sunset", term: "Blast radius", short: "Everything in the code that still depends on the feature.",
    long: "Other modules that import its files, found from the import graph. Each one must be acknowledged by an owner before outreach starts." },
  dark_launch: { group: "Sunset", term: "Canary / dark launch", short: "Switching the feature off with its flag, first for accounts that never use it, then for everyone.",
    long: "Nothing is deleted. If anyone notices, a rollback turns the flag back on." },
  scream_test: { group: "Sunset", term: "Scream test", short: "A quiet period with the feature fully off, to see if anyone complains.",
    long: "For the configured number of days: any 'on' flag evaluation, support ticket, production error or call to the feature's API routes blocks the cleanup PR." },
  damage_signal: { group: "Sunset", term: "Damage signal", short: "Production evidence that switching the feature off hurt someone.",
    long: "Errors from Sentry (or any error pipeline) and calls to the feature's retired API routes (from your gateway or logs). An admin can dismiss one as unrelated; it stays on record." },
  cleanup_pr: { group: "Sunset", term: "Cleanup PR", short: "The pull request that deletes the feature's code once the scream test passed.",
    long: "Opened on a sunset/<feature> branch with the blast radius, scream-test evidence and audit trail in its description. Merging it archives the feature; its P&L history is kept." },
};

export const hint = (key: string): string | undefined => GLOSSARY[key]?.short;
