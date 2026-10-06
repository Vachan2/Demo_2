"""asyncpg connection-pool management."""
from __future__ import annotations

import asyncpg

from config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=10,
    )


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised")
    return _pool


def pool_stats() -> dict:
    """Return a snapshot of pool utilisation for metric pushing."""
    if _pool is None:
        return {"size": 0, "idle": 0, "used": 0, "max": settings.db_pool_max_size}
    size = _pool.get_size()
    idle = _pool.get_idle_size()
    used = max(0, size - idle)
    return {
        "size": size,
        "idle": idle,
        "used": used,
        "max": settings.db_pool_max_size,
    }
