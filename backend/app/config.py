from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(v: str | None, default: bool = False) -> bool:
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # the API runs as a non-owner role so Row-Level Security is enforced
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "postgresql://featureledger_app:app_dev_password@localhost:5432/featureledger"))
    # migrations + seed run as the schema owner
    admin_database_url: str = field(default_factory=lambda: os.environ.get(
        "ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/featureledger"))
    app_db_password: str = field(default_factory=lambda: os.environ.get("APP_DB_PASSWORD", "app_dev_password"))
    migrations_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "db" / "migrations")))
    manifest_path: Path = field(default_factory=lambda: Path(os.environ.get(
        "FEATURES_MANIFEST", Path(__file__).resolve().parents[2] / "features.yaml")))
    # Phase 2: connectors read fixture APIs built from the seed company instead of live vendors
    sandbox_mode: bool = field(default_factory=lambda: _bool(os.environ.get("SANDBOX_MODE"), True))
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    seed_on_start: bool = field(default_factory=lambda: _bool(os.environ.get("SEED_ON_START"), True))
    default_org_slug: str = field(default_factory=lambda: os.environ.get("DEFAULT_ORG_SLUG", "northwind"))
    # real-data mode (SEED_ON_START=false): the organization created on first start
    org_name: str = field(default_factory=lambda: os.environ.get("ORG_NAME", "My Company"))
    # Phase 1 auth: one static bearer token (unset = open dev mode). Phase 2: OIDC + per-user roles.
    api_token: str | None = field(default_factory=lambda: os.environ.get("FL_API_TOKEN") or None)
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None)
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()))

    # ---------------------------------------------------------------- Phase 4: auth
    # the browser-facing URL of the dashboard (OIDC redirect_uri = APP_URL/api/v1/auth/oidc/callback)
    app_url: str = field(default_factory=lambda: os.environ.get("APP_URL", "http://localhost:3000").rstrip("/"))
    # HS256 key for session tokens. Required outside SANDBOX_MODE (see session_secret_or_die).
    session_secret: str | None = field(default_factory=lambda: os.environ.get("SESSION_SECRET") or None)
    session_ttl_hours: int = field(default_factory=lambda: int(os.environ.get("SESSION_TTL_HOURS", "12")))
    # dev login: one click per role. Only ever available when SANDBOX_MODE=true.
    dev_login: bool = field(default_factory=lambda: _bool(os.environ.get("AUTH_DEV_LOGIN"), True))
    oidc_provider: str = field(default_factory=lambda: os.environ.get("OIDC_PROVIDER", "").strip().lower())
    oidc_issuer: str = field(default_factory=lambda: os.environ.get("OIDC_ISSUER", "").strip())
    oidc_client_id: str = field(default_factory=lambda: os.environ.get("OIDC_CLIENT_ID", "").strip())
    oidc_client_secret: str | None = field(default_factory=lambda: os.environ.get("OIDC_CLIENT_SECRET") or None)
    oidc_scopes: str = field(default_factory=lambda: os.environ.get("OIDC_SCOPES", ""))
    oidc_role_claim: str | None = field(default_factory=lambda: os.environ.get("OIDC_ROLE_CLAIM"))
    oidc_role_map: str = field(default_factory=lambda: os.environ.get("OIDC_ROLE_MAP", ""))
    oidc_default_role: str = field(default_factory=lambda: os.environ.get("OIDC_DEFAULT_ROLE", "viewer"))
    oidc_allowed_domains: tuple[str, ...] = field(default_factory=lambda: tuple(
        d.strip().lower() for d in os.environ.get("OIDC_ALLOWED_DOMAINS", "").split(",") if d.strip()))
    # the first admins of a fresh install: these emails are made admin when they sign in with SSO
    bootstrap_admins: tuple[str, ...] = field(default_factory=lambda: tuple(
        e.strip().lower() for e in os.environ.get("BOOTSTRAP_ADMIN_EMAILS", "").split(",") if e.strip()))

    # ---------------------------------------------------------------- Phase 4: sunset execution
    # sandbox | launchdarkly | statsig ; empty = launchdarkly/statsig if their keys are set, else sandbox
    flag_provider: str = field(default_factory=lambda: os.environ.get("FLAG_PROVIDER", "").strip().lower())
    # which account attribute flag targeting uses: external_ref (Stripe customer id) or id
    flag_account_key: str = field(default_factory=lambda: os.environ.get("FLAG_ACCOUNT_KEY", "external_ref"))
    # owner/name of the repo that cleanup PRs are opened against (live mode needs GITHUB_TOKEN too)
    cleanup_repo: str | None = field(default_factory=lambda: os.environ.get("SUNSET_CLEANUP_REPO") or None)
    cleanup_base_branch: str | None = field(default_factory=lambda: os.environ.get("SUNSET_CLEANUP_BASE") or None)

    # ---------------------------------------------------------------- Phase 5: live demo
    # DEMO_MODE=true: a public "Launch Live Demo" button signs visitors into an isolated, resettable demo org
    demo_mode: bool = field(default_factory=lambda: _bool(os.environ.get("DEMO_MODE"), False))
    demo_org_slug: str = field(default_factory=lambda: os.environ.get("DEMO_ORG_SLUG", "acme-demo"))
    demo_org_name: str = field(default_factory=lambda: os.environ.get("DEMO_ORG_NAME", "Acme Cloud"))
    # a demo visitor may reset the shared demo at most once per this many seconds (FL_API_TOKEN is exempt)
    demo_reset_cooldown_s: int = field(default_factory=lambda: int(os.environ.get("DEMO_RESET_COOLDOWN_S", "60")))

    # ---------------------------------------------------------------- hardening
    rate_limit_enabled: bool = field(default_factory=lambda: _bool(os.environ.get("RATE_LIMIT_ENABLED"), True))
    rate_limit_redis: bool = field(default_factory=lambda: _bool(os.environ.get("RATE_LIMIT_REDIS"), True))
    # proxies in front of the API that append to X-Forwarded-For (1 = the Next.js dashboard)
    trusted_proxy_hops: int = field(default_factory=lambda: int(os.environ.get("TRUSTED_PROXY_HOPS", "0")))

    @property
    def dev_login_enabled(self) -> bool:
        return self.sandbox_mode and self.dev_login

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_client_id and (self.oidc_issuer or self.oidc_provider == "google"))

    @property
    def cookie_secure(self) -> bool:
        return self.app_url.startswith("https://")


MIN_SESSION_SECRET = 32


class ConfigError(RuntimeError):
    """Unsafe production configuration: the process refuses to start."""


def production_problems(cfg: Config) -> list[str]:
    """Settings that are tolerable in a sandbox but must never reach a deployment holding real data."""
    if cfg.sandbox_mode:
        return []
    problems = []
    if cfg.demo_mode:
        problems.append("DEMO_MODE=true exposes a public, login-free demo tenant; set DEMO_MODE=false")
    secret = (cfg.session_secret or "").strip()
    if not secret:
        problems.append("SESSION_SECRET is missing or blank; generate one with: openssl rand -base64 48")
    elif len(secret) < MIN_SESSION_SECRET:
        problems.append(f"SESSION_SECRET is {len(secret)} characters; use at least {MIN_SESSION_SECRET} "
                        "(openssl rand -base64 48)")
    return problems


def assert_safe_to_start(cfg: Config | None = None) -> None:
    """Fail closed: with SANDBOX_MODE=false, refuse to start on an unsafe configuration."""
    problems = production_problems(cfg or config)
    if problems:
        raise ConfigError("refusing to start with SANDBOX_MODE=false:\n  - " + "\n  - ".join(problems))


config = Config()
