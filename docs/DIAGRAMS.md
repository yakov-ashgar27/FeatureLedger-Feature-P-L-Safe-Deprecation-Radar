# FeatureLedger: Data Flow & Data Model

Drawn from the code: the 11 SQL migrations in [`db/migrations/`](../db/migrations/) (the ERD was generated from them, so table and column names match exactly) and the pipeline described in the [README](../README.md#architecture-phases-14). GitHub renders the diagrams directly.

**Contents:** [DFD Level 0](#dfd-level-0-context-diagram) · [DFD Level 1](#dfd-level-1) · [ERD overview](#erd-overview-all-37-tables-by-domain) · [Tenancy & ingestion](#1-tenancy-access--ingestion) · [Cost & value (P&L)](#2-cost--value-the-pl-inputs-and-output) · [Feature Graph](#3-feature-graph-which-code-belongs-to-which-feature) · [Safe Sunset](#4-safe-sunset-flags-cleanup-prs-and-reports)

**DFD notation:** rectangles are external entities, numbered rounded boxes are processes, cylinders are data stores.

## DFD Level 0: context diagram

```mermaid
flowchart LR
    VEN["Business systems<br/>Stripe · Jira · PostHog · Zendesk · AWS Cost Explorer"]
    GH["GitHub"]
    FLAG["Feature-flag service<br/>LaunchDarkly · Statsig"]
    IDP["Identity provider<br/>Okta · Google · Auth0"]
    USR["Product team<br/>Admin · PM/Tech lead · CS lead · Viewer"]
    SL["Slack"]
    SYS(("0<br/>FeatureLedger"))

    VEN -- "revenue, work items, usage,<br/>tickets, cloud & LLM cost" --> SYS
    GH -- "PRs, pushes, features.yaml,<br/>merge webhooks" --> SYS
    FLAG -- "flag evaluations" --> SYS
    SYS -- "flag changes" --> FLAG
    SYS -- "cleanup PR, Zombie-Guard comment" --> GH
    IDP -- "signed identity (OIDC)" --> SYS
    USR -- "decisions, approvals, settings" --> SYS
    SYS -- "feature P&L, sunset board, brief" --> USR
    SYS -- "monthly executive digest" --> SL
```

## DFD Level 1

```mermaid
flowchart TB
    VEN["Business systems"]
    GH["GitHub"]
    FLAG["Feature-flag service"]
    USR["Product team"]
    SL["Slack"]

    P1("1.0<br/>Sync connectors<br/>every 15 min + webhooks")
    P2("2.0<br/>Map code & work<br/>to features")
    P3("3.0<br/>Compute monthly<br/>feature P&L")
    P4("4.0<br/>Run Safe Sunset<br/>state machine")
    P5("5.0<br/>Guard pull requests<br/>Zombie-Guard")
    P6("6.0<br/>Report<br/>brief & digest")

    D1[("D1 Ingest inbox<br/>+ sync cursors / runs")]
    D2[("D2 Raw signals<br/>work items · accounts · usage<br/>tickets · cost events")]
    D3[("D3 Feature Graph<br/>features · code assets<br/>mappings · embeddings")]
    D4[("D4 Feature P&L<br/>feature_pnl_monthly")]
    D5[("D5 Sunset plans<br/>+ hash-chained audit log")]
    D6[("D6 Flag states<br/>+ evaluations")]

    VEN -- "vendor records" --> P1
    GH -- "PRs, pushes" --> P1
    P1 -- "scrubbed, idempotent envelopes" --> D1
    P1 -- "stitched to accounts & teams" --> D2
    GH -- "features.yaml manifest" --> P2
    D2 -- "work items, tickets" --> P2
    USR -- "confirm / reassign triage" --> P2
    P2 -- "mappings: human > manifest > structural > semantic" --> D3
    D2 -- "effort, usage, ARR, cost" --> P3
    D3 -- "attribution" --> P3
    P3 -- "costs, value, classification" --> D4
    D4 -- "zombie verdict" --> P4
    USR -- "role-gated approvals" --> P4
    FLAG -- "evaluations" --> D6
    P4 -- "flag change (fails closed)" --> FLAG
    P4 -- "state" --> D6
    P4 -- "transition + audit entry" --> D5
    P4 -- "dead-code PR" --> GH
    GH -- "PR merged webhook" --> P4
    GH -- "PR opened / updated" --> P5
    D3 -- "files to features" --> P5
    D4 -- "net P&L, adoption" --> P5
    P5 -- "comment, edited in place" --> GH
    D4 -- "top bleeding features" --> P6
    D5 -- "reclaimed zombie tax" --> P6
    P6 -- "executive brief / PDF" --> USR
    P6 -- "Block Kit digest" --> SL
```

## ERD overview: all 37 tables by domain

Every table carries `org_id` and is isolated per tenant by **PostgreSQL Row-Level Security** (`FORCE`d, app role without `BYPASSRLS`). To keep the diagrams readable, the `org_id → organizations` link is drawn only in the tenancy view. In the detailed views, dotted lines are logical references on partitioned tables (`usage_signals`, `cost_events`), which have no database-level foreign key.

```mermaid
flowchart TB
    subgraph T["1 · Tenancy & ingestion"]
        direction TB
        organizations --- org_settings
        organizations --- users
        organizations --- teams
        organizations --- integrations
        integrations --- sync_cursors
        integrations --- sync_runs
        integrations --- ingest_inbox
        integrations --- repositories
    end
    subgraph P["2 · Cost & value (P&L)"]
        direction TB
        work_items --- work_item_allocations
        accounts --- account_identities
        accounts --- account_snapshots
        accounts --- account_activity_monthly
        accounts --- usage_signals
        accounts --- support_tickets
        accounts --- account_dependencies
        cost_events
        damage_signals
        eng_capacity_monthly
        feature_pnl_monthly
    end
    subgraph G["3 · Feature Graph"]
        direction TB
        features --- code_assets_map["asset_feature_mappings"]
        code_assets --- code_assets_map
        import_edges
        manifest_versions
        mapping_suggestions
        feature_embeddings
        subject_embeddings
    end
    subgraph S["4 · Safe Sunset & reports"]
        direction TB
        sunset_plans --- sunset_audit_logs
        sunset_plans --- flag_states
        sunset_plans --- cleanup_pull_requests
        flag_evaluations
        pr_guard_comments
        executive_digests
    end
    repositories --> code_assets
    teams --> work_items
    features --> work_item_allocations
    features --> feature_pnl_monthly
    features --> usage_signals
    features --> sunset_plans
```

Each box is a table; lines inside a domain are foreign keys, arrows between domains are the main cross-domain links. The detailed ERDs below show primary keys, foreign keys and the main business columns of each table (not every column).

### 1. Tenancy, access & ingestion

```mermaid
erDiagram
    ORGANIZATIONS ||--|| ORG_SETTINGS : "org_id"
    ORGANIZATIONS ||--o{ USERS : "org_id"
    ORGANIZATIONS ||--o{ TEAMS : "org_id"
    TEAMS |o--o{ TEAMS : "parent_team_id"
    ORGANIZATIONS ||--o{ INTEGRATIONS : "org_id"
    INTEGRATIONS ||--o{ SYNC_CURSORS : "integration_id"
    ORGANIZATIONS ||--o{ SYNC_CURSORS : "org_id"
    ORGANIZATIONS ||--o{ SYNC_RUNS : "org_id"
    INTEGRATIONS ||--o{ SYNC_RUNS : "integration_id"
    ORGANIZATIONS ||--o{ INGEST_INBOX : "org_id"
    INTEGRATIONS ||--o{ INGEST_INBOX : "integration_id"
    ORGANIZATIONS ||--o{ REPOSITORIES : "org_id"
    INTEGRATIONS |o--o{ REPOSITORIES : "integration_id"
    ORGANIZATIONS {
        uuid id PK
        text name
        text slug
        bool is_demo
    }
    ORG_SETTINGS {
        uuid org_id PK,FK
        numeric eng_hourly_rate_usd
        text default_attribution_model
        numeric margin_bleed_max_gross_margin
    }
    USERS {
        uuid id PK
        uuid org_id FK
        text email
        user_role role
        text oidc_subject
        int session_epoch
    }
    TEAMS {
        uuid id PK
        uuid org_id FK
        text key
        text name
        int headcount
        uuid parent_team_id FK
    }
    INTEGRATIONS {
        uuid id PK
        uuid org_id FK
        integration_kind kind
        integration_status status
        bytea token_ciphertext
        timestamptz last_synced_at
        bool sandbox
    }
    SYNC_CURSORS {
        uuid integration_id PK,FK
        uuid org_id FK
        text entity_type PK
        text cursor
    }
    SYNC_RUNS {
        bigint id PK
        uuid org_id FK
        uuid integration_id FK
        sync_status status
        int envelopes_new
        int dead_lettered
    }
    INGEST_INBOX {
        text idempotency_key PK
        uuid org_id FK
        uuid integration_id FK
        text entity_type
        text external_id
        jsonb payload
        timestamptz processed_at
        timestamptz dead_lettered_at
    }
    REPOSITORIES {
        uuid id PK
        uuid org_id FK
        uuid integration_id FK
        text full_name
        text manifest_sha
    }
```

### 2. Cost & value: the P&L inputs and output

```mermaid
erDiagram
    TEAMS |o--o{ FEATURES : "owner_team_id"
    TEAMS |o--o{ TEAMS : "parent_team_id"
    TEAMS |o--o{ WORK_ITEMS : "team_id"
    WORK_ITEMS ||--o{ WORK_ITEM_ALLOCATIONS : "work_item_id"
    FEATURES ||--o{ WORK_ITEM_ALLOCATIONS : "feature_id"
    TEAMS ||--o{ ENG_CAPACITY_MONTHLY : "team_id"
    ACCOUNTS ||--o{ ACCOUNT_IDENTITIES : "account_id"
    ACCOUNTS ||--o{ ACCOUNT_SNAPSHOTS : "account_id"
    ACCOUNTS ||--o{ ACCOUNT_ACTIVITY_MONTHLY : "account_id"
    ACCOUNTS |o--o{ SUPPORT_TICKETS : "account_id"
    FEATURES |o--o{ SUPPORT_TICKETS : "feature_id"
    FEATURES |o--o{ DAMAGE_SIGNALS : "feature_id"
    FEATURES ||--o{ ACCOUNT_DEPENDENCIES : "feature_id"
    ACCOUNTS ||--o{ ACCOUNT_DEPENDENCIES : "account_id"
    FEATURES ||--o{ FEATURE_PNL_MONTHLY : "feature_id"
    FEATURES ||..o{ USAGE_SIGNALS : "feature_id (logical)"
    ACCOUNTS ||..o{ USAGE_SIGNALS : "account_id (logical)"
    FEATURES ||..o{ COST_EVENTS : "feature_id (logical, NULL = untagged)"
    FEATURES {
        uuid id PK
        text key
        text name
        feature_status status
        uuid owner_team_id FK
        text flag_key
        text tier_gate
    }
    TEAMS {
        uuid id PK
        text key
        text name
        int headcount
        uuid parent_team_id FK
    }
    WORK_ITEMS {
        uuid id PK
        work_source source
        work_kind kind
        uuid team_id FK
        uuid repo_id FK
        date period_month
        numeric raw_effort_hours
    }
    WORK_ITEM_ALLOCATIONS {
        uuid work_item_id PK,FK
        uuid feature_id PK,FK
        numeric share
        mapping_source source
    }
    ENG_CAPACITY_MONTHLY {
        uuid org_id PK,FK
        uuid team_id PK,FK
        date period_month PK
        numeric capacity_hours
        numeric calibration_factor
    }
    ACCOUNTS {
        uuid id PK
        text name
        text plan_tier
        numeric arr_usd
        date renewal_date
    }
    ACCOUNT_IDENTITIES {
        uuid account_id FK
        uuid org_id PK,FK
        integration_kind system PK
        text external_id PK
    }
    ACCOUNT_SNAPSHOTS {
        uuid org_id PK,FK
        uuid account_id PK,FK
        date period_month PK
        numeric arr_usd
        text plan_tier
    }
    ACCOUNT_ACTIVITY_MONTHLY {
        uuid org_id PK,FK
        date period_month PK
        uuid account_id PK,FK
        int active_days
    }
    USAGE_SIGNALS {
        uuid org_id PK
        date period_month PK
        uuid account_id PK
        uuid feature_id PK
        bigint events
        int active_users
    }
    SUPPORT_TICKETS {
        uuid id PK
        uuid account_id FK
        uuid feature_id FK
        numeric mapping_confidence
        text priority
        int handle_minutes
    }
    COST_EVENTS {
        bigint id PK
        uuid org_id PK
        date period_month PK
        cost_type cost_type
        numeric amount_usd
        text unit
    }
    DAMAGE_SIGNALS {
        uuid id PK
        uuid feature_id FK
        text kind
        int occurrences
    }
    ACCOUNT_DEPENDENCIES {
        uuid org_id PK,FK
        uuid feature_id PK,FK
        uuid account_id PK,FK
        date period_month PK
        numeric dependency_score
        numeric churn_risk
        numeric attributed_mrr
    }
    FEATURE_PNL_MONTHLY {
        uuid org_id PK,FK
        uuid feature_id PK,FK
        date period_month PK
        numeric c_eng
        numeric c_support
        numeric c_infra
        numeric c_total
        numeric v_usage
        numeric v_tier
        numeric revenue_at_risk_arr
        feature_class classification
        numeric c_ai_tokens
        numeric gross_margin_pct
        bool margin_bleed
    }
```

`feature_pnl_monthly` is the computed result: one row per feature per month, with cost lines (engineering, support, infra, AI tokens), value lines (usage, tier) and the classification (e.g. Zombie, Growth Bet).

### 3. Feature Graph: which code belongs to which feature

```mermaid
erDiagram
    REPOSITORIES |o--o{ CODE_ASSETS : "repo_id"
    CODE_ASSETS ||--o{ ASSET_FEATURE_MAPPINGS : "asset_id"
    FEATURES ||--o{ ASSET_FEATURE_MAPPINGS : "feature_id"
    USERS |o--o{ ASSET_FEATURE_MAPPINGS : "decided_by"
    REPOSITORIES ||--o{ IMPORT_EDGES : "repo_id"
    FEATURES |o--o{ MAPPING_SUGGESTIONS : "suggested_feature_id"
    FEATURES |o--o{ MAPPING_SUGGESTIONS : "decided_feature_id"
    USERS |o--o{ MAPPING_SUGGESTIONS : "decided_by"
    FEATURES ||--o{ FEATURE_EMBEDDINGS : "feature_id"
    FEATURES {
        uuid id PK
        text key
        text name
        feature_status status
        uuid owner_team_id FK
        text flag_key
        text tier_gate
    }
    REPOSITORIES {
        uuid id PK
        uuid integration_id FK
        text full_name
        text manifest_sha
    }
    CODE_ASSETS {
        uuid id PK
        uuid repo_id FK
        asset_type asset_type
        text identifier
    }
    ASSET_FEATURE_MAPPINGS {
        uuid asset_id PK,FK
        uuid feature_id PK,FK
        mapping_source source
        numeric confidence
        mapping_status status
        uuid decided_by FK
    }
    IMPORT_EDGES {
        uuid repo_id PK,FK
        text from_path PK
        text to_path PK
    }
    MANIFEST_VERSIONS {
        bigint id PK
        text repo
        text commit_sha
        text status
    }
    MAPPING_SUGGESTIONS {
        uuid id PK
        triage_subject subject_type
        uuid suggested_feature_id FK
        numeric confidence
        mapping_status status
        uuid decided_feature_id FK
        uuid decided_by FK
        int tier
    }
    FEATURE_EMBEDDINGS {
        uuid feature_id PK,FK
        text model
        vector embedding
    }
    SUBJECT_EMBEDDINGS {
        uuid org_id PK,FK
        triage_subject subject_type PK
        uuid subject_id PK
        vector embedding
    }
    USERS {
        uuid id PK
        text email
        user_role role
        text oidc_subject
        int session_epoch
    }
```

### 4. Safe Sunset: flags, cleanup PRs and reports

```mermaid
erDiagram
    FEATURES ||--o{ SUNSET_PLANS : "feature_id"
    USERS |o--o{ SUNSET_PLANS : "created_by"
    SUNSET_PLANS ||--o{ SUNSET_AUDIT_LOGS : "plan_id"
    USERS |o--o{ SUNSET_AUDIT_LOGS : "actor_id"
    SUNSET_PLANS |o--o{ FLAG_STATES : "plan_id"
    USERS |o--o{ FLAG_STATES : "updated_by"
    SUNSET_PLANS ||--o{ CLEANUP_PULL_REQUESTS : "plan_id"
    USERS |o--o{ EXECUTIVE_DIGESTS : "created_by"
    FEATURES {
        uuid id PK
        text key
        text name
        feature_status status
        uuid owner_team_id FK
        text flag_key
        text tier_gate
    }
    USERS {
        uuid id PK
        text email
        user_role role
        text oidc_subject
        int session_epoch
    }
    SUNSET_PLANS {
        uuid id PK
        uuid feature_id FK
        sunset_state state
        text canary_phase
        int scream_test_days
        jsonb blast_radius
        uuid created_by FK
        int version
        int rollback_count
    }
    SUNSET_AUDIT_LOGS {
        bigint id PK
        uuid plan_id FK
        sunset_state from_state
        sunset_state to_state
        text action
        uuid actor_id FK
        bytea prev_hash
        bytea hash
    }
    FLAG_STATES {
        uuid org_id PK,FK
        text flag_key PK
        text provider
        text state
        uuid plan_id FK
        uuid updated_by FK
    }
    FLAG_EVALUATIONS {
        uuid org_id PK,FK
        text flag_key PK
        timestamptz bucket_start PK
        text source PK
        bigint on_count
        bigint off_count
    }
    CLEANUP_PULL_REQUESTS {
        uuid id PK
        uuid plan_id FK
        text repo
        int number
        text state
        timestamptz merged_at
    }
    PR_GUARD_COMMENTS {
        uuid org_id PK,FK
        text repo PK
        int pr_number PK
        text head_sha
        bool flagged
    }
    EXECUTIVE_DIGESTS {
        uuid id PK
        date period_month
        text slack_status
        uuid created_by FK
    }
```

`sunset_audit_logs` is append-only and hash-chained (`prev_hash` → `hash`), so any edit to history is detectable.
