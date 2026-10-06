"""
User-service FastAPI application.

Endpoints
---------
GET  /users/{user_id}/profile   → fetch user profile (cache-backed)
GET  /health                    → liveness check
GET  /metrics/raw               → current metric snapshot
POST /admin/failure             → toggle failure mode at runtime
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from pythonjsonlogger import jsonlogger

from cache import cache_stats, close_cache, get_redis, init_cache
from config import settings
from database import close_pool, init_pool, pool_stats
from metrics import record_request, reset_counters, snapshot
from user_service import get_user_profile

# ── Logging ───────────────────────────────────────────────────────────────────
handler = logging.StreamHandler()
handler.setFormatter(
    jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger("user-service")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="User Service", version=settings.app_version)

_metric_push_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup() -> None:
    await init_pool()
    await init_cache()
    # Warm the cache with a few synthetic entries so the healthy demo
    # shows a real hit rate before the bug is injected.
    await _warm_cache()
    global _metric_push_task
    _metric_push_task = asyncio.create_task(_push_metrics_loop())
    log.info("user-service started", extra={"version": settings.app_version})


@app.on_event("shutdown")
async def shutdown() -> None:
    if _metric_push_task:
        _metric_push_task.cancel()
    await close_cache()
    await close_pool()


async def _warm_cache() -> None:
    """Pre-populate Redis with v1 keys so the healthy hit rate is visible."""
    import json

    redis = get_redis()
    for uid in range(1, 21):
        key = f"user:{uid}:profile:v1"
        profile = {
            "id": uid,
            "username": f"user{uid}",
            "email": f"user{uid}@example.com",
            "full_name": f"Demo User {uid}",
            "created_at": "2024-01-01T00:00:00",
        }
        await redis.setex(key, settings.cache_ttl_seconds, json.dumps(profile))
    log.info("cache warmed", extra={"keys": 20})


# ── Middleware: record every request ──────────────────────────────────────────
@app.middleware("http")
async def metrics_middleware(request: Request, call_next) -> Response:
    t0 = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - t0
    # Cache hit tracking is done inside the route handler; here we track
    # overall request stats (pass cache_hit=False as sentinel for middleware,
    # the real value is recorded in the route handler itself).
    if request.url.path.startswith("/users/"):
        record_request(
            duration_s=duration,
            status_code=response.status_code,
            cache_hit=False,    # overridden below in the route handler via state
            db_hit=False,
        )
    return response


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/users/{user_id}/profile")
async def get_profile(user_id: str, request: Request) -> dict:
    t0 = time.monotonic()
    try:
        int(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id must be an integer")

    try:
        profile, cache_hit = await get_user_profile(user_id)
    except Exception as exc:
        log.error(
            "profile fetch failed",
            extra={"user_id": user_id, "error": str(exc)},
        )
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    if profile is None:
        raise HTTPException(status_code=404, detail="User not found")

    duration = time.monotonic() - t0
    # Record with accurate cache/db flags (replaces the middleware stub)
    record_request(
        duration_s=duration,
        status_code=200,
        cache_hit=cache_hit,
        db_hit=not cache_hit,
    )

    return {
        "data": profile,
        "meta": {
            "cache_hit": cache_hit,
            "source": "redis" if cache_hit else "postgresql",
        },
    }


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "failure_mode": settings.demo_failure_mode,
    }


@app.get("/metrics/raw")
async def metrics_raw() -> dict:
    cs = await cache_stats()
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "metrics": snapshot(),
        "pool": pool_stats(),
        "cache": cs,
        "failure_mode": settings.demo_failure_mode,
    }


@app.post("/admin/failure")
async def set_failure_mode(
    mode: str = Query(..., description="none | cache_key_regression")
) -> dict:
    allowed = {"none", "cache_key_regression"}
    if mode not in allowed:
        raise HTTPException(
            status_code=400, detail=f"mode must be one of {allowed}"
        )
    settings.demo_failure_mode = mode

    # When switching to the buggy mode, bump the version to simulate a deploy
    if mode == "cache_key_regression":
        settings.app_version = "1.4.0"
        log.warning(
            "FAILURE INJECTED: cache key regression",
            extra={"version": settings.app_version, "mode": mode},
        )
    else:
        settings.app_version = "1.3.0"
        # Re-warm cache so healthy mode shows a good hit rate immediately
        await _warm_cache()
        reset_counters()
        log.info(
            "failure mode cleared",
            extra={"version": settings.app_version, "mode": mode},
        )

    return {"ok": True, "failure_mode": mode, "version": settings.app_version}


# ── Background metric pusher ──────────────────────────────────────────────────
async def _push_metrics_loop() -> None:
    """Push a metric snapshot to Sentinel every N seconds."""
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                cs = await cache_stats()
                payload = {
                    "service": settings.app_name,
                    "version": settings.app_version,
                    "metrics": snapshot(),
                    "pool": pool_stats(),
                    "cache": cs,
                    "failure_mode": settings.demo_failure_mode,
                }
                await client.post(
                    f"{settings.sentinel_url}/api/v1/metrics",
                    json=payload,
                )
            except Exception as exc:
                log.debug("metric push failed", extra={"error": str(exc)})
            await asyncio.sleep(settings.metrics_push_interval)
