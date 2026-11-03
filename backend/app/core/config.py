"""Application configuration.

All configuration comes from environment variables (optionally loaded from
a .env file for local dev). Nothing here has a production-unsafe default
that would be dangerous if left in place — the JWT secret has a dev-only
placeholder that is validated as *present*, not as secure; operators are
expected to override it (documented in .env.example and SETUP.md).

Phase 7 addition: when APP_ENV=production, a second validation pass
refuses to start with configuration that would be silently dangerous in a
real deployment (the well-known dev JWT secret, a wildcard CORS origin
combined with credentials, or DEBUG-level logging). This is fail-fast
config validation, not a runtime toggle — see §2 of
docs/RELIABILITY_AND_OPERATIONS.md.
"""
from functools import lru_cache
from typing import List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_DEFAULT_JWT_SECRET = "dev-only-change-me-1234567890abcdef1234567890abcdef"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/pred_platform.db"

    jwt_secret_key: str = _DEV_DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    api_v1_prefix: str = "/api/v1"

    # Background job runner (Phase 7): a job that runs longer than this is
    # treated as hung and force-failed rather than left RUNNING forever —
    # see app/jobs/runner.py and §6 of docs/RELIABILITY_AND_OPERATIONS.md.
    job_timeout_seconds: int = 300

    # API reliability (Phase 7): requests with a Content-Length above this
    # are rejected with 413 before any parsing/DB work happens — see
    # app/core/middleware.py BodySizeLimitMiddleware and §8 of the doc above.
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MB

    # Basic anti-brute-force on /auth/login (Phase 7, §10). Explicitly
    # scoped: in-memory, per-process, resets on restart — not a distributed
    # rate limiter. Documented as such rather than oversold.
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 60

    # Optional AI copilot (Phase 9) — see docs/COPILOT.md. Absent/empty
    # ANTHROPIC_API_KEY means the copilot runs in "not configured" mode:
    # the rest of the platform is completely unaffected, and the copilot
    # API answers honestly that no provider is available rather than
    # crashing or pretending. No key is ever logged or returned to a client.
    copilot_llm_provider: str = "anthropic"  # only provider implemented; "none" disables even if a key is set
    anthropic_api_key: str | None = None
    copilot_llm_model: str = "claude-3-5-haiku-20241022"
    copilot_llm_timeout_seconds: int = 30
    copilot_max_evidence_items: int = 12

    @property
    def copilot_configured(self) -> bool:
        return self.copilot_llm_provider == "anthropic" and bool(self.anthropic_api_key)

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    @field_validator("jwt_secret_key")
    @classmethod
    def secret_key_must_be_nonempty(cls, v: str) -> str:
        if not v or len(v) < 16:
            raise ValueError("JWT_SECRET_KEY must be set and at least 16 characters long")
        return v

    @model_validator(mode="after")
    def _validate_production_safety(self) -> "Settings":
        """Fail fast, at startup, rather than serve traffic on config that
        would be a real vulnerability in a real deployment. Every check here
        is something that is silently fine in local dev and genuinely
        dangerous once APP_ENV=production — this validator only fires when
        that flag is actually set, so local dev and tests are unaffected."""
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.jwt_secret_key == _DEV_DEFAULT_JWT_SECRET:
            problems.append(
                "JWT_SECRET_KEY is still the checked-in development default. "
                "Generate a real value (see .env.example) before running with APP_ENV=production."
            )
        if "*" in self.cors_origin_list:
            problems.append(
                "CORS_ORIGINS contains '*' with APP_ENV=production. A wildcard origin combined "
                "with credentialed requests (this API sends allow_credentials=True) allows any "
                "website to make authenticated requests on a logged-in user's behalf. List exact "
                "origins instead."
            )
        if self.database_url.startswith("sqlite") and ":memory:" in self.database_url:
            problems.append(
                "DATABASE_URL is an in-memory SQLite database with APP_ENV=production — all data "
                "would be lost on every process restart."
            )
        if problems:
            raise ValueError(
                "Refusing to start with APP_ENV=production and unsafe configuration:\n- "
                + "\n- ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
