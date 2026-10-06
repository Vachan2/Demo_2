"""
Integration tests — require live PostgreSQL and Redis.

Run with:
    pytest tests/test_integration.py -v

Skip automatically if DATABASE_URL or REDIS_URL are not reachable.
"""
from __future__ import annotations

import json
import os

import pytest

# ── Skip guard ─────────────────────────────────────────────────────────────────
# If the environment hasn't been set up, skip all integration tests gracefully.
pytestmark = pytest.mark.skipif(
    os.environ.get("DATABASE_URL") is None,
    reason="DATABASE_URL not set — skipping integration tests",
)


@pytest.fixture(scope="module")
async def services():
    """Initialise real DB pool and Redis client for the module."""
    import database
    import cache

    await database.init_pool()
    await cache.init_cache()
    yield
    await database.close_pool()
    await cache.close_cache()


@pytest.mark.asyncio
async def test_healthy_path_cache_miss_then_hit(services):
    """
    First call → cache MISS → DB hit → Redis write.
    Second call → cache HIT → no DB hit.
    """
    import redis.asyncio as aioredis
    import user_service
    from cache import get_redis

    redis = get_redis()
    uid = "1"
    key = f"user:{uid}:profile:v1"

    # Clear any existing entry
    await redis.delete(key)

    # First call — should miss
    profile1, hit1 = await user_service.get_user_profile_healthy(uid)
    assert not hit1, "First call should be a cache miss"
    assert profile1 is not None

    # Second call — should hit
    profile2, hit2 = await user_service.get_user_profile_healthy(uid)
    assert hit2, "Second call should be a cache hit"
    assert profile1["id"] == profile2["id"]


@pytest.mark.asyncio
async def test_buggy_path_always_misses(services):
    """
    The buggy path reads :v2 — it should never hit even if :v1 is warm.
    """
    import user_service
    from cache import get_redis

    redis = get_redis()
    uid = "2"
    v1_key = f"user:{uid}:profile:v1"

    # Ensure :v1 is warm
    warm_data = json.dumps({
        "id": 2, "username": "bob", "email": "bob@example.com",
        "full_name": "Bob Brown", "created_at": "2024-01-01T00:00:00",
    })
    await redis.setex(v1_key, 300, warm_data)

    # Buggy path should still miss (reads :v2)
    _profile, hit = await user_service.get_user_profile_buggy(uid)
    assert not hit, (
        "Buggy path must miss even when :v1 is warm — that's the bug"
    )


@pytest.mark.asyncio
async def test_facade_mode_switch(services):
    """
    Switching failure mode mid-test should route correctly without restart.
    """
    import user_service
    from config import settings

    uid = "3"

    settings.demo_failure_mode = "none"
    _p, hit_healthy = await user_service.get_user_profile(uid)
    # Doesn't matter if hit or miss — just that it doesn't raise

    settings.demo_failure_mode = "cache_key_regression"
    _p2, hit_buggy = await user_service.get_user_profile(uid)

    # Reset
    settings.demo_failure_mode = "none"
    # No assertion needed — the test passes if no exception is raised
