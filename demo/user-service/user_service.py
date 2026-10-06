"""
User-profile service — healthy and buggy implementations.

The Healthy implementation uses the cache key:
    user:{user_id}:profile:v1

Deployment v1.4.0 introduced a "cache optimisation" that accidentally
changed the key to:
    user:{user_id}:profile:v2

Because all existing cached objects live under :v1, every request
becomes a cache miss, hammering PostgreSQL.

The public get_user_profile() facade routes to the correct implementation
based on settings.demo_failure_mode at request time — no restart required.
"""
from __future__ import annotations

import json
import time

from cache import get_redis
from config import settings
from database import get_pool
from metrics import record_db_query_time

# ── Cache-key constants ───────────────────────────────────────────────────────

CACHE_KEY_VERSION_HEALTHY = "v1"
CACHE_KEY_VERSION_BUGGY = "v2"       # introduced in v1.4.0


def _healthy_cache_key(user_id: str) -> str:
    return f"user:{user_id}:profile:{CACHE_KEY_VERSION_HEALTHY}"


def _buggy_cache_key(user_id: str) -> str:
    # Bug: v2 doesn't match anything in the cache populated under v1
    return f"user:{user_id}:profile:{CACHE_KEY_VERSION_BUGGY}"


# ── Database helper ───────────────────────────────────────────────────────────

async def _fetch_from_db(user_id: str) -> dict | None:
    """Hit PostgreSQL and return user profile dict (or None)."""
    pool = get_pool()
    t0 = time.monotonic()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, email, full_name, created_at "
            "FROM users WHERE id = $1",
            int(user_id),
        )
    elapsed = time.monotonic() - t0
    record_db_query_time(elapsed)
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "full_name": row["full_name"],
        "created_at": row["created_at"].isoformat(),
    }


# ── Healthy implementation ────────────────────────────────────────────────────

async def get_user_profile_healthy(user_id: str) -> tuple[dict | None, bool]:
    """
    Return (profile, cache_hit).

    Cache key: user:{user_id}:profile:v1
    """
    redis = get_redis()
    key = _healthy_cache_key(user_id)

    cached = await redis.get(key)
    if cached is not None:
        return json.loads(cached), True          # cache HIT

    # Cache MISS → database
    profile = await _fetch_from_db(user_id)
    if profile:
        await redis.setex(key, settings.cache_ttl_seconds, json.dumps(profile))
    return profile, False


# ── Buggy implementation (v1.4.0) ────────────────────────────────────────────

async def get_user_profile_buggy(user_id: str) -> tuple[dict | None, bool]:
    """
    Return (profile, cache_hit).

    Bug: cache key uses :v2, but all warm entries live under :v1.
    Every request is a cache miss → PostgreSQL gets 100% of the load.
    """
    redis = get_redis()
    key = _buggy_cache_key(user_id)           # ← the regression

    cached = await redis.get(key)
    if cached is not None:
        return json.loads(cached), True

    # Cache MISS → database (always, because key doesn't match)
    profile = await _fetch_from_db(user_id)
    if profile:
        # Writes to :v2 — so future requests still miss if they check :v2,
        # but warm :v1 entries remain untouched in Redis.
        await redis.setex(key, settings.cache_ttl_seconds, json.dumps(profile))
    return profile, False


# ── Public facade ─────────────────────────────────────────────────────────────

async def get_user_profile(user_id: str) -> tuple[dict | None, bool]:
    """
    Route to healthy or buggy implementation based on current failure mode.
    Failure mode is read at call time so it can be toggled at runtime.
    """
    if settings.demo_failure_mode == "cache_key_regression":
        return await get_user_profile_buggy(user_id)
    return await get_user_profile_healthy(user_id)
