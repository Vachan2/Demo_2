"""Sentinel configuration."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Detection thresholds ─────────────────────────────────────────────────
    # Trip an incident when cache hit rate drops below this
    cache_hit_rate_warn_pct: float = 50.0
    # Trip when DB queries/sec exceed this
    db_queries_per_sec_warn: float = 100.0
    # Trip when p95 latency exceeds this
    latency_p95_warn_ms: float = 1000.0
    # Trip when error rate exceeds this
    error_rate_warn_pct: float = 5.0

    # ── Sentinel identity ────────────────────────────────────────────────────
    sentinel_version: str = "2.0.0"

    # ── OpenAI (optional — falls back to rule-based RCA if absent) ───────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # ── GitHub (optional — falls back to local git if absent) ────────────────
    github_token: str = ""
    github_repo: str = ""          # e.g. "org/demo2"

    # ── Local paths ──────────────────────────────────────────────────────────
    # Absolute or relative path to the user-service source tree
    local_repo_path: str = "../demo/user-service"

    # ── User-service ─────────────────────────────────────────────────────────
    user_api_url: str = "http://localhost:9001"


settings = Settings()
