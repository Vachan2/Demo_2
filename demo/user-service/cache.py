"""Redis cache client."""
from __future__ import annotations

import redis.asyncio as aioredis

from config import settings

_redis: aioredis.Redis | None = None


async def init_cache() -> None:
    global _redis
    _redis = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


async def close_cache() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis client not initialised")
    return _redis


async def cache_stats() -> dict:
    """Return cache operational stats for metric pushing."""
    if _redis is None:
        return {"available": False, "connected_clients": 0, "used_memory_mb": 0.0}
    try:
        info = await _redis.info("all")
        return {
            "available": True,
            "connected_clients": info.get("connected_clients", 0),
            "used_memory_mb": round(
                info.get("used_memory", 0) / 1_048_576, 2
            ),
            "keyspace_hits": info.get("keyspace_hits", 0),
            "keyspace_misses": info.get("keyspace_misses", 0),
        }
    except Exception:
        return {"available": False}
