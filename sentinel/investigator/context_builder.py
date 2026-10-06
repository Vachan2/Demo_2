"""
Context builder for the cache-stampede investigator.

Assembles a structured investigation context containing:
  • The incident's symptoms and triggering metrics
  • A simulated deployment history (the smoking gun is in v1.4.0)
  • Full source code of the relevant user-service files
  • Analysis hints that guide the LLM toward causal reasoning
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from config import settings
from models import Incident

log = logging.getLogger("sentinel.investigator.context_builder")

# ── Simulated deployment history ──────────────────────────────────────────────
# In a real system this would come from a CI/CD API or git log.
# The v1.4.0 entry is the deliberate red herring / smoking gun.

_DEPLOYMENT_HISTORY: list[dict] = [
    {
        "version": "1.0.0",
        "deployed_at": "2024-09-01T09:00:00Z",
        "deployed_by": "ci-pipeline",
        "summary": "Initial release of user-service with Redis caching.",
        "diff_summary": (
            "Added user profile endpoint. "
            "Cache key: `user:{user_id}:profile:v1`. "
            "TTL = 300s."
        ),
        "status": "stable",
    },
    {
        "version": "1.1.0",
        "deployed_at": "2024-10-15T14:23:00Z",
        "deployed_by": "ci-pipeline",
        "summary": "Added email field to profile response.",
        "diff_summary": (
            "Extended SELECT query in _fetch_from_db. "
            "Cache serialisation unchanged."
        ),
        "status": "stable",
    },
    {
        "version": "1.2.0",
        "deployed_at": "2024-11-20T11:05:00Z",
        "deployed_by": "ci-pipeline",
        "summary": "Performance: increased DB pool max_size from 10 to 20.",
        "diff_summary": (
            "db_pool_max_size: 10 → 20. "
            "Improved throughput under load."
        ),
        "status": "stable",
    },
    {
        "version": "1.3.0",
        "deployed_at": "2025-01-08T08:45:00Z",
        "deployed_by": "ci-pipeline",
        "summary": "Added full_name field; structured JSON logging.",
        "diff_summary": (
            "SELECT extended to include full_name. "
            "python-json-logger added. "
            "Cache key unchanged: `user:{user_id}:profile:v1`."
        ),
        "status": "stable",
    },
    {
        "version": "1.4.0",
        "deployed_at": "2026-10-06T07:12:00Z",    # today — matches demo date
        "deployed_by": "ci-pipeline",
        "summary": "Cache optimisation: versioned cache keys for zero-downtime invalidation.",
        "diff_summary": (
            "CHANGED cache key from `user:{user_id}:profile:v1` "
            "to `user:{user_id}:profile:v2`. "
            "Intended to support future schema migrations without stale reads. "
            "NOTE: existing cached entries remain under :v1 and are never read again."
        ),
        "risk_level": "MEDIUM",
        "status": "deployed_today",   # this is the incident-triggering deploy
    },
]

# Source files to include in context
_SOURCE_FILES = [
    "user_service.py",
    "cache.py",
    "config.py",
]


def build(incident: Incident) -> dict:
    """
    Assemble a full investigation context for the given incident.

    Returns a dict that is either serialised into an LLM prompt or
    inspected directly by the rule-based fallback.
    """
    repo_path = Path(settings.local_repo_path).resolve()

    # ── Source code ──────────────────────────────────────────────────────────
    source_files: dict[str, str] = {}
    for filename in _SOURCE_FILES:
        fpath = repo_path / filename
        try:
            source_files[filename] = fpath.read_text(encoding="utf-8")
            log.debug("loaded source file", extra={"file": filename})
        except FileNotFoundError:
            source_files[filename] = f"<file not found: {fpath}>"
            log.warning("source file not found", extra={"file": str(fpath)})

    # ── Symptoms (structured from triggering metrics) ─────────────────────────
    m = incident.triggering_metrics
    symptoms = {
        "cache_hit_rate_pct": m.cache_hit_rate_pct,
        "db_queries_per_sec": m.db_queries_per_sec,
        "latency_p95_ms": m.latency_p95_ms,
        "error_rate_pct": m.error_rate_pct,
        "cache_hits_total": m.cache_hits_total,
        "cache_misses_total": m.cache_misses_total,
        "db_query_latency_avg_ms": m.db_query_latency_avg_ms,
    }

    # ── Baseline (what healthy looks like) ────────────────────────────────────
    baseline = {
        "cache_hit_rate_pct": 96.0,
        "db_queries_per_sec": 8.0,
        "latency_p95_ms": 35.0,
        "error_rate_pct": 0.0,
    }

    # ── Analysis hints ────────────────────────────────────────────────────────
    # These guide the LLM toward causal, layered reasoning — not just
    # "the database is slow".
    analysis_hints = [
        "Redis is AVAILABLE (connected_clients > 0, used_memory > 0). "
        "The cache infrastructure itself is not the failure.",
        "Cache hit rate dropped from ~96% to near 0% immediately after the "
        "latest deployment. The timing is not a coincidence.",
        "When cache hit rate is 0%, every API request becomes a DB query. "
        "At 100 req/s that is 100 DB queries/s instead of 4-8.",
        "The database is NOT the root cause — it is a secondary victim of "
        "the cache not being used. Ask WHY the cache stopped working.",
        "Look at the deployment diff for v1.4.0 carefully. "
        "A cache key version change means existing warm entries are orphaned.",
        "The fix is in user_service.py: change the cache key back to :v1, "
        "or ensure the warm-up step writes :v2 entries before traffic is served.",
    ]

    ctx = {
        "incident_id": incident.id,
        "service": incident.service,
        "triggering_version": incident.triggering_version,
        "severity": incident.severity.value,
        "symptoms": symptoms,
        "baseline": baseline,
        "delta": {
            k: {"before": baseline[k], "after": symptoms[k]}
            for k in baseline
            if k in symptoms
        },
        "redis_available": True,   # confirmed because the service is running
        "deployment_history": _DEPLOYMENT_HISTORY,
        "recent_deployment": _DEPLOYMENT_HISTORY[-1],
        "source_files": source_files,
        "analysis_hints": analysis_hints,
    }

    log.info(
        "investigation context built",
        extra={
            "incident_id": incident.id,
            "source_files": list(source_files.keys()),
            "deployments": len(_DEPLOYMENT_HISTORY),
        },
    )
    return ctx
