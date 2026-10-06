"""
Async load generator for post-fix verification.

Sends a configurable number of concurrent requests to the user-service
profile endpoint and collects latency + error statistics.  A parallel
coroutine polls /metrics/raw to capture cache hit rate and DB query rate.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from config import settings


@dataclass
class LoadTestResult:
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    error_rate_pct: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_avg_ms: float = 0.0
    peak_cache_hit_rate_pct: float = 0.0
    avg_cache_hit_rate_pct: float = 0.0
    peak_db_queries_per_sec: float = 0.0
    avg_db_queries_per_sec: float = 0.0
    duration_s: float = 0.0
    raw_latencies_ms: list[float] = field(default_factory=list)


_PROFILE_IDS = list(range(1, 21))   # user IDs 1–20


async def _sample_metrics(
    client: httpx.AsyncClient,
    samples: list[dict],
    stop_event: asyncio.Event,
    interval: float = 0.5,
) -> None:
    """Poll /metrics/raw every *interval* seconds until stop_event is set."""
    while not stop_event.is_set():
        try:
            r = await client.get(
                f"{settings.user_api_url}/metrics/raw", timeout=3
            )
            if r.status_code == 200:
                samples.append(r.json())
        except Exception:
            pass
        await asyncio.sleep(interval)


async def run(
    num_requests: int = 120,
    concurrency: int = 20,
    ramp_up: bool = True,
) -> LoadTestResult:
    """
    Send *num_requests* concurrent GET requests to /users/{id}/profile.

    Returns a LoadTestResult with latency percentiles and cache metrics.
    """
    latencies_ms: list[float] = []
    errors: int = 0
    metric_samples: list[dict] = []
    stop_event = asyncio.Event()

    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(client: httpx.AsyncClient, uid: int) -> None:
        nonlocal errors
        async with semaphore:
            t0 = time.monotonic()
            try:
                r = await client.get(
                    f"{settings.user_api_url}/users/{uid}/profile",
                    timeout=10,
                )
                elapsed_ms = (time.monotonic() - t0) * 1000
                latencies_ms.append(elapsed_ms)
                if r.status_code >= 500:
                    errors += 1
            except Exception:
                elapsed_ms = (time.monotonic() - t0) * 1000
                latencies_ms.append(elapsed_ms)
                errors += 1

    t_start = time.monotonic()

    async with httpx.AsyncClient() as client:
        # Start metric sampling in background
        sampler = asyncio.create_task(
            _sample_metrics(client, metric_samples, stop_event)
        )

        # Optional gentle ramp-up to avoid cold-start skew
        tasks = []
        for i in range(num_requests):
            uid = _PROFILE_IDS[i % len(_PROFILE_IDS)]
            tasks.append(asyncio.create_task(fetch(client, uid)))
            if ramp_up and i < 20:
                await asyncio.sleep(0.02)   # 20ms between first 20 requests

        await asyncio.gather(*tasks)

        stop_event.set()
        await sampler

    duration_s = time.monotonic() - t_start
    total = len(latencies_ms)

    if not latencies_ms:
        return LoadTestResult(duration_s=duration_s)

    sorted_lat = sorted(latencies_ms)
    n = len(sorted_lat)

    # Cache hit rate from samples
    cache_rates = [
        s.get("metrics", {}).get("cache_hit_rate_pct", 0.0)
        for s in metric_samples
        if s.get("metrics")
    ]
    db_qps_vals = [
        s.get("metrics", {}).get("db_queries_per_sec", 0.0)
        for s in metric_samples
        if s.get("metrics")
    ]

    return LoadTestResult(
        total_requests=total,
        successful=total - errors,
        failed=errors,
        error_rate_pct=round((errors / total) * 100, 1),
        latency_p50_ms=round(sorted_lat[int(n * 0.50)], 1),
        latency_p95_ms=round(sorted_lat[int(n * 0.95)], 1),
        latency_avg_ms=round(sum(sorted_lat) / n, 1),
        peak_cache_hit_rate_pct=round(max(cache_rates, default=0.0), 1),
        avg_cache_hit_rate_pct=round(
            sum(cache_rates) / len(cache_rates) if cache_rates else 0.0, 1
        ),
        peak_db_queries_per_sec=round(max(db_qps_vals, default=0.0), 1),
        avg_db_queries_per_sec=round(
            sum(db_qps_vals) / len(db_qps_vals) if db_qps_vals else 0.0, 1
        ),
        duration_s=round(duration_s, 2),
        raw_latencies_ms=sorted_lat,
    )
