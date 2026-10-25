"""Application settings, read from environment variables.

Secrets (SECRET_KEY, APP_PASSWORD, DATABASE_URL) are never hard-coded.
For local development they can be placed in a `.env` file (git-ignored);
on Render they are set in the service's Environment settings.
"""
from __future__ import annotations

import os
import secrets
import sys

try:  # optional: load .env for local development
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


class ConfigError(RuntimeError):
    pass


def _bool(e, name: str, default: bool = False) -> bool:
    v = e.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(e, name: str, default: int) -> int:
    try:
        return int(e.get(name, default))
    except ValueError:
        raise ConfigError(f"{name} must be a whole number.")


def normalize_db_url(url: str) -> str:
    """Render provides postgres://...; SQLAlchemy + psycopg 3 needs
    postgresql+psycopg://..."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings:
    def __init__(self, env: dict | None = None):
        e = os.environ if env is None else env
        get = e.get

        # Production when running on Render (RENDER=true is set by Render)
        # or when APP_ENV=production is set explicitly.
        self.production = (get("APP_ENV", "").lower() == "production") or (get("RENDER", "").lower() == "true")

        raw_db = get("DATABASE_URL", "").strip()
        if not raw_db:
            if self.production:
                raise ConfigError("DATABASE_URL is not set.")
            os.makedirs("instance", exist_ok=True)
            raw_db = "sqlite:///instance/clinical_round_dev.db"
        self.database_url = normalize_db_url(raw_db)
        if self.production and self.database_url.startswith("sqlite"):
            raise ConfigError("SQLite is for local development only; use PostgreSQL in production.")

        self.secret_key = get("SECRET_KEY", "").strip()
        if not self.secret_key:
            if self.production:
                raise ConfigError("SECRET_KEY is not set.")
            self.secret_key = secrets.token_urlsafe(48)
            print("WARNING: SECRET_KEY not set - using a temporary key (sessions reset on restart).",
                  file=sys.stderr)
        elif self.production and len(self.secret_key) < 32:
            raise ConfigError("SECRET_KEY must be at least 32 characters.")

        # Single-user sign-in. Required whenever the app is hosted
        # (production). Locally it may be left empty to skip the sign-in.
        self.app_password = get("APP_PASSWORD", "")
        if self.production and len(self.app_password) < 10:
            raise ConfigError("APP_PASSWORD must be set and at least 10 characters long.")

        # Retention policy
        self.completed_retention_days = _int(e, "COMPLETED_ROUND_RETENTION_DAYS", 7)
        self.inactive_round_days = _int(e, "INACTIVE_ROUND_DAYS", 30)
        self.cleanup_interval_minutes = _int(e, "CLEANUP_INTERVAL_MINUTES", 10)

        # Sessions
        self.session_hours = _int(e, "SESSION_HOURS", 12)
        self.secure_cookies = _bool(e, "SECURE_COOKIES", self.production)

        # Limits
        self.max_photo_mb = 15
        self.max_photos_per_observation = 12
        self.max_observations_per_round = 300
        self.max_text_chars = 20_000


def load_settings() -> Settings:
    return Settings()
