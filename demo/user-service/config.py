"""User-service configuration via environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Identity ────────────────────────────────────────────────────────────
    app_name: str = "user-service"
    app_version: str = "1.3.0"          # bumped to v1.4.0 when bug is injected

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = "postgresql://demo:demo@localhost:5433/userdb"
    db_pool_min_size: int = 2
    db_pool_max_size: int = 20

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6380"
    cache_ttl_seconds: int = 300        # 5 minutes

    # ── Failure injection ────────────────────────────────────────────────────
    # "none"                → healthy (cache_key uses :v1)
    # "cache_key_regression" → buggy  (cache_key uses :v2, always misses)
    demo_failure_mode: str = "none"

    # ── Sentinel reporting ───────────────────────────────────────────────────
    sentinel_url: str = "http://localhost:8002"
    metrics_push_interval: float = 2.0


settings = Settings()
