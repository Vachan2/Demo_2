"""In-process metrics store for the user-service."""
from __future__ import annotations

import time
from collections import deque
from threading import Lock

_lock = Lock()

# Rolling window of the last 200 request observations
_window: deque[dict] = deque(maxlen=200)

# Cache hit / miss counters (reset-able for clean demo stages)
_cache_hits: int = 0
_cache_misses: int = 0

# DB query counter
_db_queries: int = 0
_db_query_times: deque[float] = deque(maxlen=200)   # seconds each query took

# Track window start for queries/sec calculation
_window_start: float = time.monotonic()


def record_request(
    duration_s: float,
    status_code: int,
    cache_hit: bool,
    db_hit: bool,
) -> None:
    global _cache_hits, _cache_misses, _db_queries
    with _lock:
        _window.append(
            {
                "ts": time.monotonic(),
                "duration_s": duration_s,
                "status_code": status_code,
                "cache_hit": cache_hit,
                "db_hit": db_hit,
            }
        )
        if cache_hit:
            _cache_hits += 1
        else:
            _cache_misses += 1
        if db_hit:
            _db_queries += 1


def record_db_query_time(duration_s: float) -> None:
    with _lock:
        _db_query_times.append(duration_s)


def snapshot() -> dict:
    """Compute current metric snapshot from the rolling window."""
    with _lock:
        now = time.monotonic()
        window_list = list(_window)
        db_times = list(_db_query_times)
        hits = _cache_hits
        misses = _cache_misses
        queries = _db_queries
        elapsed = max(now - _window_start, 1.0)

    if not window_list:
        return {
            "requests_per_sec": 0.0,
            "error_rate_pct": 0.0,
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
            "latency_avg_ms": 0.0,
            "cache_hit_rate_pct": 0.0,
            "cache_hits_total": hits,
            "cache_misses_total": misses,
            "db_queries_per_sec": 0.0,
            "db_query_latency_avg_ms": 0.0,
        }

    durations_ms = sorted(r["duration_s"] * 1000 for r in window_list)
    errors = sum(1 for r in window_list if r["status_code"] >= 500)
    n = len(durations_ms)

    # Requests per second (based on oldest item in window)
    oldest_ts = min(r["ts"] for r in window_list)
    span = max(now - oldest_ts, 1.0)
    rps = round(n / span, 1)

    # Cache hit rate
    total = hits + misses
    hit_rate = round((hits / total) * 100, 1) if total > 0 else 0.0

    # DB queries/sec
    db_qps = round(queries / elapsed, 1)

    # DB query latency
    db_lat_avg = round(
        (sum(db_times) / len(db_times)) * 1000, 1
    ) if db_times else 0.0

    return {
        "requests_per_sec": rps,
        "error_rate_pct": round((errors / n) * 100, 1),
        "latency_p50_ms": round(durations_ms[int(n * 0.50)], 1),
        "latency_p95_ms": round(durations_ms[int(n * 0.95)], 1),
        "latency_avg_ms": round(sum(durations_ms) / n, 1),
        "cache_hit_rate_pct": hit_rate,
        "cache_hits_total": hits,
        "cache_misses_total": misses,
        "db_queries_per_sec": db_qps,
        "db_query_latency_avg_ms": db_lat_avg,
    }


def reset_counters() -> None:
    """Reset cache-hit counters — call when switching failure modes."""
    global _cache_hits, _cache_misses, _db_queries, _window_start
    with _lock:
        _cache_hits = 0
        _cache_misses = 0
        _db_queries = 0
        _window.clear()
        _db_query_times.clear()
        _window_start = time.monotonic()
