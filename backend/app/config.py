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
    seed_on_start: bool = field(default_factory=lambda: _bool(os.environ.get("SEED_ON_START"), True))
    default_org_slug: str = field(default_factory=lambda: os.environ.get("DEFAULT_ORG_SLUG", "northwind"))
    # Phase 1 auth: one static bearer token (unset = open dev mode). Phase 2: OIDC + per-user roles.
    api_token: str | None = field(default_factory=lambda: os.environ.get("FL_API_TOKEN") or None)
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None)
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()))


config = Config()
