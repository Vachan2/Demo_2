"""
Anomaly detector for Demo 2.

Evaluates an incoming MetricsPayload against configured thresholds and
creates an Incident when any threshold is breached.

Thresholds that matter for the cache-stampede scenario
------------------------------------------------------
1. cache_hit_rate_pct  < cache_hit_rate_warn_pct   (e.g. < 50 %)
2. db_queries_per_sec  > db_queries_per_sec_warn    (e.g. > 100 q/s)
3. latency_p95_ms      > latency_p95_warn_ms        (e.g. > 1000 ms)
4. error_rate_pct      > error_rate_warn_pct        (e.g. > 5 %)

Any single breach opens a new incident (one per service at a time).
"""
from __future__ import annotations

import logging

from config import settings
from incident_store import has_open_incident, next_incident_id, save
from models import Incident, IncidentStatus, MetricsPayload, Severity

log = logging.getLogger("sentinel.detector")


def evaluate(payload: MetricsPayload) -> Incident | None:
    """
    Inspect *payload* and return a new Incident if a threshold is breached
    and no open incident exists for this service, otherwise return None.
    """
    if has_open_incident(payload.service):
        return None

    m = payload.metrics
    breaches: list[str] = []

    # ── Cache hit rate ───────────────────────────────────────────────────────
    if m.cache_hit_rate_pct < settings.cache_hit_rate_warn_pct and (
        m.cache_hits_total + m.cache_misses_total > 10        # ignore cold start
    ):
        breaches.append(
            f"cache_hit_rate_pct={m.cache_hit_rate_pct:.1f}% "
            f"(threshold < {settings.cache_hit_rate_warn_pct}%)"
        )

    # ── DB query rate ────────────────────────────────────────────────────────
    if m.db_queries_per_sec > settings.db_queries_per_sec_warn:
        breaches.append(
            f"db_queries_per_sec={m.db_queries_per_sec:.1f} "
            f"(threshold > {settings.db_queries_per_sec_warn})"
        )

    # ── API latency ──────────────────────────────────────────────────────────
    if m.latency_p95_ms > settings.latency_p95_warn_ms:
        breaches.append(
            f"latency_p95_ms={m.latency_p95_ms:.0f}ms "
            f"(threshold > {settings.latency_p95_warn_ms:.0f}ms)"
        )

    # ── Error rate ───────────────────────────────────────────────────────────
    if m.error_rate_pct > settings.error_rate_warn_pct:
        breaches.append(
            f"error_rate_pct={m.error_rate_pct:.1f}% "
            f"(threshold > {settings.error_rate_warn_pct}%)"
        )

    if not breaches:
        return None

    incident = Incident(
        id=next_incident_id(),
        service=payload.service,
        severity=_severity(m.error_rate_pct, m.latency_p95_ms),
        status=IncidentStatus.DETECTING,
        triggering_metrics=m,
        triggering_version=payload.version,
        triggering_failure_mode=payload.failure_mode,
    )
    incident.timeline.append(
        f"Incident opened — {len(breaches)} threshold breach(es)"
    )
    for breach in breaches:
        incident.timeline.append(f"  ✗ {breach}")

    save(incident)

    log.warning(
        "incident opened",
        extra={
            "incident_id": incident.id,
            "service": incident.service,
            "version": payload.version,
            "breaches": breaches,
        },
    )
    return incident


def _severity(error_rate_pct: float, latency_p95_ms: float) -> Severity:
    if error_rate_pct > 20 or latency_p95_ms > 5000:
        return Severity.CRITICAL
    if error_rate_pct > 10 or latency_p95_ms > 3000:
        return Severity.HIGH
    if error_rate_pct > 5 or latency_p95_ms > 1000:
        return Severity.MEDIUM
    return Severity.LOW
