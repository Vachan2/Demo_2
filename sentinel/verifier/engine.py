"""
Post-fix verification engine for Demo 2.

Runs a load test against the (now-patched) user-service and compares
key metrics against the "before" values captured at incident creation.

Resolution criteria (ALL must pass)
------------------------------------
- cache_hit_rate_pct  ≥  85 %      (was ≈ 0 %, target ≈ 96 %)
- db_queries_per_sec  ≤  30        (was ≈ 250–500)
- latency_p95_ms      ≤  200 ms    (was ≈ 3–7 s)
- error_rate_pct      ≤  1 %       (was > 5–30 %)
"""
from __future__ import annotations

import logging

from incident_store import save
from models import (
    Incident,
    IncidentStatus,
    MetricComparison,
    VerificationResult,
)
from verifier.load_test import LoadTestResult, run as run_load_test

log = logging.getLogger("sentinel.verifier")

# ── Thresholds for "resolved" determination ───────────────────────────────────
_CACHE_HIT_RATE_MIN   = 85.0    # %
_DB_QUERIES_PER_SEC_MAX = 30.0  # q/s
_LATENCY_P95_MAX_MS   = 200.0   # ms
_ERROR_RATE_MAX_PCT   = 1.0     # %


def _before_snapshot(incident: Incident) -> dict:
    """
    Reconstruct the "before" metric dict from the incident's triggering metrics.
    These are the values that caused the alert.
    """
    m = incident.triggering_metrics
    return {
        "cache_hit_rate_pct":   m.cache_hit_rate_pct,
        "db_queries_per_sec":   m.db_queries_per_sec,
        "latency_p95_ms":       m.latency_p95_ms,
        "error_rate_pct":       m.error_rate_pct,
    }


def _after_snapshot(result: LoadTestResult) -> dict:
    return {
        "cache_hit_rate_pct":   result.avg_cache_hit_rate_pct,
        "db_queries_per_sec":   result.avg_db_queries_per_sec,
        "latency_p95_ms":       result.latency_p95_ms,
        "error_rate_pct":       result.error_rate_pct,
    }


def _build_comparisons(before: dict, after: dict) -> list[MetricComparison]:
    specs = [
        # (key, unit, higher_is_better)
        ("cache_hit_rate_pct",   "%",     True),
        ("db_queries_per_sec",   "q/s",   False),
        ("latency_p95_ms",       "ms",    False),
        ("error_rate_pct",       "%",     False),
    ]
    comparisons: list[MetricComparison] = []
    for key, unit, higher_is_better in specs:
        b = before.get(key, 0.0)
        a = after.get(key, 0.0)
        if higher_is_better:
            improved = a > b
        else:
            improved = a < b
        comparisons.append(
            MetricComparison(
                metric=key,
                before=b,
                after=a,
                improved=improved,
                unit=unit,
            )
        )
    return comparisons


def _resolution_criteria_met(after: dict) -> tuple[bool, list[str]]:
    """Check each resolution criterion; return (all_met, list_of_failures)."""
    failures: list[str] = []

    if after["cache_hit_rate_pct"] < _CACHE_HIT_RATE_MIN:
        failures.append(
            f"cache_hit_rate_pct={after['cache_hit_rate_pct']:.1f}% "
            f"(required ≥ {_CACHE_HIT_RATE_MIN}%)"
        )
    if after["db_queries_per_sec"] > _DB_QUERIES_PER_SEC_MAX:
        failures.append(
            f"db_queries_per_sec={after['db_queries_per_sec']:.1f} "
            f"(required ≤ {_DB_QUERIES_PER_SEC_MAX})"
        )
    if after["latency_p95_ms"] > _LATENCY_P95_MAX_MS:
        failures.append(
            f"latency_p95_ms={after['latency_p95_ms']:.0f}ms "
            f"(required ≤ {_LATENCY_P95_MAX_MS:.0f}ms)"
        )
    if after["error_rate_pct"] > _ERROR_RATE_MAX_PCT:
        failures.append(
            f"error_rate_pct={after['error_rate_pct']:.1f}% "
            f"(required ≤ {_ERROR_RATE_MAX_PCT}%)"
        )

    return len(failures) == 0, failures


def _format_summary(
    incident: Incident,
    before: dict,
    after: dict,
    passed: bool,
    failures: list[str],
) -> str:
    rows = []
    rows.append(f"{'METRIC':<30} {'BEFORE':>12} {'AFTER':>12} {'STATUS':>8}")
    rows.append("─" * 66)

    specs = [
        ("cache_hit_rate_pct",  "%",   True),
        ("db_queries_per_sec",  "q/s", False),
        ("latency_p95_ms",      "ms",  False),
        ("error_rate_pct",      "%",   False),
    ]
    for key, unit, higher_is_better in specs:
        b = before.get(key, 0.0)
        a = after.get(key, 0.0)
        improved = (a > b) if higher_is_better else (a < b)
        icon = "✅" if improved else "❌"
        rows.append(
            f"{key:<30} {b:>10.1f}{unit}  {a:>10.1f}{unit}  {icon}"
        )

    rows.append("")
    if passed:
        rows.append(
            f"✅ {incident.id} RESOLVED — all resolution criteria met"
        )
    else:
        rows.append(f"❌ {incident.id} NOT RESOLVED — criteria failed:")
        for f in failures:
            rows.append(f"   • {f}")

    return "\n".join(rows)


async def verify(incident: Incident) -> VerificationResult:
    """
    Run a post-fix load test and determine whether the incident is resolved.

    Transitions the incident to VERIFICATION_PASSED → RESOLVED or FAILED.
    """
    log.info("verification started", extra={"incident_id": incident.id})

    before = _before_snapshot(incident)

    # Run the load test
    try:
        load_result: LoadTestResult = await run_load_test(
            num_requests=120,
            concurrency=20,
        )
    except Exception as exc:
        log.error(
            "load test failed",
            extra={"incident_id": incident.id, "error": str(exc)},
        )
        result = VerificationResult(
            passed=False,
            before=before,
            after={},
            summary=f"Load test failed: {exc}",
        )
        incident.transition(
            IncidentStatus.FAILED,
            f"verification load test error: {exc}",
        )
        save(incident)
        return result

    after = _after_snapshot(load_result)
    comparisons = _build_comparisons(before, after)
    passed, failures = _resolution_criteria_met(after)
    summary = _format_summary(incident, before, after, passed, failures)

    result = VerificationResult(
        passed=passed,
        before=before,
        after=after,
        comparisons=comparisons,
        summary=summary,
    )

    if passed:
        incident.verification = result
        incident.transition(
            IncidentStatus.VERIFICATION_PASSED,
            f"cache_hit_rate={after['cache_hit_rate_pct']:.1f}% "
            f"db_qps={after['db_queries_per_sec']:.1f} "
            f"p95={after['latency_p95_ms']:.0f}ms",
        )
        incident.transition(
            IncidentStatus.RESOLVED,
            "all resolution criteria satisfied",
        )
    else:
        incident.verification = result
        incident.transition(
            IncidentStatus.FAILED,
            f"resolution criteria not met: {'; '.join(failures)}",
        )

    save(incident)

    log.info(
        "verification complete",
        extra={
            "incident_id": incident.id,
            "passed": passed,
            "cache_hit_rate_after": after.get("cache_hit_rate_pct"),
            "db_qps_after": after.get("db_queries_per_sec"),
            "p95_after": after.get("latency_p95_ms"),
        },
    )

    return result
