"""
Unit tests for user_service.py.

Tests:
  - Cache key constants and format
  - Healthy implementation: cache miss → DB → cache write
  - Healthy implementation: cache hit (no DB call)
  - Buggy implementation: always misses (:v2 key)
  - Facade routing by failure mode
  - _fetch_from_db: missing user returns None
"""
from __future__ import annotations

import json
import unittest.mock as mock

import pytest

import user_service


class TestCacheKeyConstants:
    def test_healthy_key_version_is_v1(self):
        assert user_service.CACHE_KEY_VERSION_HEALTHY == "v1"

    def test_healthy_cache_key_format(self):
        key = user_service._healthy_cache_key("42")
        assert key == "user:42:profile:v1"

    def test_buggy_cache_key_format(self):
        key = user_service._buggy_cache_key("42")
        assert key == "user:42:profile:v2"

    def test_healthy_and_buggy_keys_are_different(self):
        uid = "5"
        assert user_service._healthy_cache_key(uid) != user_service._buggy_cache_key(uid)


class TestGetUserProfileHealthy:
    @pytest.mark.asyncio
    async def test_cache_miss_fetches_from_db(self, fake_redis, fake_pool, sample_profile):
        fake_redis.get.side_effect = [None]  # always miss for this test
        fake_redis.setex = mock.AsyncMock(return_value=True)

        with mock.patch("user_service.get_redis", return_value=fake_redis):
            with mock.patch("user_service.get_pool", return_value=fake_pool):
                profile, hit = await user_service.get_user_profile_healthy("1")

        assert hit is False
        assert profile is not None
        assert profile["username"] == sample_profile["username"]
        fake_pool.acquire.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_miss_writes_to_redis(self, fake_redis, fake_pool, sample_profile):
        fake_redis.get.side_effect = [None]

        with mock.patch("user_service.get_redis", return_value=fake_redis):
            with mock.patch("user_service.get_pool", return_value=fake_pool):
                await user_service.get_user_profile_healthy("1")

        fake_redis.setex.assert_called_once()
        args = fake_redis.setex.call_args[0]
        # key ends with :v1
        assert args[0].endswith(":v1")

    @pytest.mark.asyncio
    async def test_cache_hit_skips_db(self, fake_redis, fake_pool, sample_profile):
        # Return serialised profile on first get (cache hit)
        fake_redis.get.side_effect = [json.dumps(sample_profile)]

        with mock.patch("user_service.get_redis", return_value=fake_redis):
            with mock.patch("user_service.get_pool", return_value=fake_pool):
                profile, hit = await user_service.get_user_profile_healthy("1")

        assert hit is True
        assert profile["id"] == sample_profile["id"]
        # Pool should NOT have been touched
        fake_pool.acquire.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_user(self, fake_redis):
        fake_redis.get.side_effect = [None]

        pool = mock.MagicMock()
        conn = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=None)  # user not found
        cm = mock.AsyncMock()
        cm.__aenter__ = mock.AsyncMock(return_value=conn)
        cm.__aexit__  = mock.AsyncMock(return_value=False)
        pool.acquire  = mock.MagicMock(return_value=cm)

        with mock.patch("user_service.get_redis", return_value=fake_redis):
            with mock.patch("user_service.get_pool", return_value=pool):
                profile, hit = await user_service.get_user_profile_healthy("999")

        assert profile is None
        assert hit is False


class TestGetUserProfileBuggy:
    """The buggy implementation always misses because it uses :v2."""

    @pytest.mark.asyncio
    async def test_always_misses_existing_v1_entries(self, fake_pool):
        """
        Even if Redis has a :v1 entry, the buggy path queries :v2 — always a miss.
        """
        redis = mock.AsyncMock()
        # Simulate: :v1 key exists in Redis, :v2 does not
        async def selective_get(key):
            if key.endswith(":v1"):
                return json.dumps({"id": 1})
            return None                          # :v2 always misses

        redis.get.side_effect = selective_get
        redis.setex = mock.AsyncMock()

        with mock.patch("user_service.get_redis", return_value=redis):
            with mock.patch("user_service.get_pool", return_value=fake_pool):
                _profile, hit = await user_service.get_user_profile_buggy("1")

        assert hit is False, (
            "Buggy path must always miss because it reads :v2, not :v1"
        )
        # DB must have been queried
        fake_pool.acquire.assert_called_once()

    @pytest.mark.asyncio
    async def test_buggy_writes_v2_key(self, fake_pool):
        """The buggy path writes under :v2, perpetuating the mismatch."""
        redis = mock.AsyncMock()
        redis.get.return_value = None
        written_keys: list[str] = []

        async def capture_setex(key, ttl, value):
            written_keys.append(key)

        redis.setex.side_effect = capture_setex

        with mock.patch("user_service.get_redis", return_value=redis):
            with mock.patch("user_service.get_pool", return_value=fake_pool):
                await user_service.get_user_profile_buggy("1")

        assert len(written_keys) == 1
        assert written_keys[0].endswith(":v2"), (
            f"Buggy key should end with :v2, got {written_keys[0]}"
        )


class TestGetUserProfileFacade:
    @pytest.mark.asyncio
    async def test_facade_routes_to_healthy_by_default(self):
        with mock.patch.object(
            user_service, "get_user_profile_healthy",
            return_value=(None, False),
        ) as mh:
            with mock.patch.object(
                user_service, "settings",
                demo_failure_mode="none",
            ):
                await user_service.get_user_profile("1")
                mh.assert_called_once_with("1")

    @pytest.mark.asyncio
    async def test_facade_routes_to_buggy_on_regression_mode(self):
        with mock.patch.object(
            user_service, "get_user_profile_buggy",
            return_value=(None, False),
        ) as mb:
            with mock.patch.object(
                user_service, "settings",
                demo_failure_mode="cache_key_regression",
            ):
                await user_service.get_user_profile("1")
                mb.assert_called_once_with("1")
