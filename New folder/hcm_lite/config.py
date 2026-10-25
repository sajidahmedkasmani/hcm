"""Application settings loaded from environment variables (and an optional .env file).

Secrets (JWT secret, AI key, DB password) live only in the environment; commit `.env.example` only.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides variables already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./hcm_lite.db"
    jwt_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    jwt_expires_minutes: int = 8 * 60
    bcrypt_rounds: int = 12
    login_max_failures: int = 5            # per email within the window
    login_max_failures_per_ip: int = 20    # per client IP within the window
    login_window_minutes: int = 15
    timezone: str = "Asia/Karachi"
    admin_email: str | None = None
    admin_password: str | None = None
    company_name: str | None = None
    cors_origins: tuple[str, ...] = ("*",)
    auto_migrate: bool = True
    # --- AI (M8) -------------------------------------------------------------------------
    ai_api_key: str | None = None
    ai_base_url: str = "https://api.groq.com/openai/v1"
    ai_model: str = "openai/gpt-oss-120b"
    ai_timeout_seconds: float = 15.0
    ai_rate_limit_per_hour: int = 20
    ai_offline_fallback: bool = True

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        if env_file:
            _load_dotenv(Path(env_file))
        defaults = cls()
        origins = os.getenv("CORS_ORIGINS")
        return cls(
            database_url=os.getenv("DATABASE_URL", defaults.database_url),
            jwt_secret=os.getenv("JWT_SECRET") or defaults.jwt_secret,
            jwt_expires_minutes=int(os.getenv("JWT_EXPIRES_MINUTES", defaults.jwt_expires_minutes)),
            bcrypt_rounds=int(os.getenv("BCRYPT_ROUNDS", defaults.bcrypt_rounds)),
            login_max_failures=int(os.getenv("LOGIN_MAX_FAILURES", defaults.login_max_failures)),
            login_max_failures_per_ip=int(os.getenv("LOGIN_MAX_FAILURES_PER_IP", defaults.login_max_failures_per_ip)),
            login_window_minutes=int(os.getenv("LOGIN_WINDOW_MINUTES", defaults.login_window_minutes)),
            timezone=os.getenv("APP_TIMEZONE", defaults.timezone),
            admin_email=os.getenv("ADMIN_EMAIL") or None,
            admin_password=os.getenv("ADMIN_PASSWORD") or None,
            company_name=os.getenv("COMPANY_NAME") or None,
            cors_origins=tuple(o.strip() for o in origins.split(",")) if origins else defaults.cors_origins,
            auto_migrate=_env_bool("AUTO_MIGRATE", defaults.auto_migrate),
            ai_api_key=os.getenv("AI_API_KEY") or None,
            ai_base_url=os.getenv("AI_BASE_URL", defaults.ai_base_url),
            ai_model=os.getenv("AI_MODEL", defaults.ai_model),
            ai_timeout_seconds=float(os.getenv("AI_TIMEOUT_SECONDS", defaults.ai_timeout_seconds)),
            ai_rate_limit_per_hour=int(os.getenv("AI_RATE_LIMIT_PER_HOUR", defaults.ai_rate_limit_per_hour)),
            ai_offline_fallback=_env_bool("AI_OFFLINE_FALLBACK", defaults.ai_offline_fallback),
        )
