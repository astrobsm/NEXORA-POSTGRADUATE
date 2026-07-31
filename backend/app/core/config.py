"""Application configuration.

Every setting is overridable through environment variables prefixed with ``RTC_``
or through a ``.env`` file, so that a deployment never requires a code change.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RTC_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Identity -------------------------------------------------------
    app_name: str = "Postgraduate Medical Training Console"
    app_short_name: str = "RTC"
    env: Literal["local", "development", "staging", "production"] = "local"
    api_v1_prefix: str = "/api/v1"

    # ---- Vendor ---------------------------------------------------------
    vendor_name: str = "NEXORA Technologies"
    vendor_url: str = "https://github.com/astrobsm/NEXORA-POSTGRADUATE"
    vendor_statement: str = "Created and managed by NEXORA Technologies."

    # ---- Database -------------------------------------------------------
    # SQLite by default so the platform runs with zero infrastructure.
    # Production sets e.g. postgresql+psycopg://rtc:pw@postgres:5432/rtc
    database_url: str = f"sqlite:///{(BACKEND_ROOT / 'rtc.db').as_posix()}"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    #: Force the serverless connection strategy. Auto-detected when unset — see
    #: ``is_serverless`` below.
    db_serverless: bool | None = None

    # ---- Security -------------------------------------------------------
    secret_key: str = "dev-only-insecure-secret-change-me"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    jwt_algorithm: str = "HS256"
    password_min_length: int = 12
    mfa_issuer: str = "Residency Training Console"
    # Failed-login lockout
    max_failed_logins: int = 6
    lockout_minutes: int = 15

    # ---- CORS -----------------------------------------------------------
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:4173",
            "http://localhost:8080",
        ]
    )

    # ---- Object storage (S3 compatible) ---------------------------------
    s3_endpoint_url: str | None = None
    s3_bucket: str = "rtc-artifacts"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    # ---- Feature flags --------------------------------------------------
    allow_demo_seed: bool = True
    enable_ai_assistant: bool = False
    ai_provider_base_url: str | None = None
    ai_api_key: str | None = None
    enable_websockets: bool = True

    # ---- Offline sync ---------------------------------------------------
    sync_page_size: int = 500
    sync_max_clock_skew_seconds: int = 300

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_serverless(self) -> bool:
        """Whether this process is a short-lived serverless function.

        It matters because the correct database strategy inverts. A long-running
        container wants a connection pool; a serverless function is frozen and
        thawed unpredictably, so a pooled connection it holds is very likely dead
        by the time it is reused — and thousands of concurrent functions each
        holding a pool will exhaust the database's connection limit.
        """
        if self.db_serverless is not None:
            return self.db_serverless
        return bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

    @property
    def uses_pgbouncer(self) -> bool:
        """Supabase's transaction pooler listens on 6543 and cannot serve
        prepared statements, which psycopg uses by default after five executions
        of the same query. Detecting it lets the engine disable them."""
        return ":6543" in self.database_url or "pgbouncer=true" in self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
