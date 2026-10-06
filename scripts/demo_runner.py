#!/usr/bin/env python3
"""
Sentinel Demo 2 — Interactive Demo Runner
==========================================
10-stage guided walkthrough of the Redis cache stampede / cascading
DB overload scenario.

Usage
-----
    python scripts/demo_runner.py

Prerequisites
-------------
    make services          # starts Postgres + Redis via docker-compose
    make sentinel &        # starts Sentinel on :8002  (run in a separate terminal)
    make user-service &    # starts user-service on :9001 (run in a separate terminal)

Stages
------
 1  Environment check         — verify both services are reachable
 2  Baseline health           — show healthy metrics (cache ~96%, DB ~8 q/s)
 3  Simulate load             — warm the cache with real traffic
 4  Inject bug                — POST /admin/failure?mode=cache_key_regression
 5  Observe degradation       — watch cache hit rate fall, DB queries climb
 6  Sentinel detects          — poll until INC-001 appears
 7  Sentinel investigates     — poll until ROOT_CAUSE_IDENTIFIED
 8  Sentinel remediates       — poll until REMEDIATION_CREATED
 9  Sentinel validates        — poll until PATCH_VALIDATED
10  Sentinel verifies         — poll until RESOLVED, show before/after table
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import httpx

# ── Config ────────────────────────────────────────────────────────────────────
USER_SVC = "http://localhost:9001"
SENTINEL  = "http://localhost:8002"
POLL_INTERVAL = 2.0          # seconds between status polls
POLL_TIMEOUT  = 300.0        # max seconds to wait per stage

# ANSI colours
R  = "\033[0;31m"
G  = "\033[0;32m"
Y  = "\033[0;33m"
C  = "\033[0;36m"
B  = "\033[1;34m"
W  = "\033[1;37m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD  = "\033[1m"


# ── Helpers ───────────────────────────────────────────────────────────────────

def header(stage: int, title: str) -> None:
    print(f"\n{B}{'─' * 64}{RESET}")
    print(f"{B}  Stage {stage:02d} — {title}{RESET}")
    print(f"{B}{'─' * 64}{RESET}")


def ok(msg: str) -> None:
    print(f"  {G}✓{RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"  {Y}⚠{RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"  {R}✗{RESET}  {msg}")


def info(msg: str) -> None:
    print(f"  {DIM}•{RESET}  {msg}")


def metric_line(label: str, value: Any, unit: str = "", good: bool = True) -> None:
    colour = G if good else R
    print(f"    {label:<35} {colour}{value}{unit}{RESET}")


def pause(prompt: str = "Press ENTER to continue…") -> None:
    try:
        input(f"\n  {DIM}{prompt}{RESET}\n")
    except (EOFError, KeyboardInterrupt):
        print()


def _fmt_json(data: dict) -> str:
    return json.dumps(data, indent=2)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def get(client: httpx.AsyncClient, url: str) -> dict:
    r = await client.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


async def post(client: httpx.AsyncClient, url: str, **kwargs) -> dict:
    r = await client.post(url, timeout=10, **kwargs)
    r.raise_for_status()
    return r.json()


async def wait_for_incident_status(
    client: httpx.AsyncClient,
    incident_id: str,
    target_statuses: list[str],
    timeout: float = POLL_TIMEOUT,
) -> dict:
    """Poll GET /api/v1/incidents/{id} until status is in *target_statuses*."""
    deadline = time.monotonic() + timeout
    dots = 0
    while time.monotonic() < deadline:
        try:
            inc = await get(client, f"{SENTINEL}/api/v1/incidents/{incident_id}")
            status = inc.get("status", "")
            print(
                f"  {DIM}  polling {incident_id} … status={status}{' ' * max(0, 30 - len(status))}\r{RESET}",
                end="",
                flush=True,
            )
            if status in target_statuses:
                print()  # newline after carriage-return line
                return inc
            if status == "FAILED":
                print()
                raise RuntimeError(f"Incident {incident_id} reached FAILED state")
        except httpx.HTTPError:
            pass
        await asyncio.sleep(POLL_INTERVAL)
        dots += 1
    raise TimeoutError(
        f"Timed out waiting for {incident_id} to reach {target_statuses}"
    )


async def first_open_incident(client: httpx.AsyncClient) -> dict | None:
    """Return the first non-terminal incident, or None."""
    incidents = await get(client, f"{SENTINEL}/api/v1/incidents")
    terminal = {"RESOLVED", "FAILED"}
    for inc in incidents:
        if inc["status"] not in terminal:
            return inc
    # Also accept newly RESOLVED for the final stage
    if incidents:
        return incidents[0]
    return None


# ── Load helper ───────────────────────────────────────────────────────────────

async def send_load(
    client: httpx.AsyncClient,
    n: int = 40,
    concurrency: int = 10,
) -> tuple[int, int]:
    """Fire n requests; return (successes, errors)."""
    sem = asyncio.Semaphore(concurrency)
    ok_count = 0
    err_count = 0

    async def fetch(uid: int) -> None:
        nonlocal ok_count, err_count
        async with sem:
            try:
                r = await client.get(
                    f"{USER_SVC}/users/{(uid % 20) + 1}/profile",
                    timeout=10,
                )
                if r.status_code < 500:
                    ok_count += 1
                else:
                    err_count += 1
            except Exception:
                err_count += 1

    await asyncio.gather(*[asyncio.create_task(fetch(i)) for i in range(n)])
    return ok_count, err_count


# ── Stages ────────────────────────────────────────────────────────────────────

async def stage_1_env_check(client: httpx.AsyncClient) -> None:
    header(1, "Environment Check")

    services = [
        ("user-service", f"{USER_SVC}/health"),
        ("Sentinel",     f"{SENTINEL}/health"),
    ]
    all_ok = True
    for name, url in services:
        try:
            data = await get(client, url)
            ok(f"{name}  version={data.get('version', '?')}  status={data.get('status', '?')}")
        except Exception as exc:
            fail(f"{name} not reachable — {exc}")
            all_ok = False

    if not all_ok:
        print(f"\n  {R}One or more services are not running.{RESET}")
        print(f"  Run {C}make services{RESET}, {C}make sentinel{RESET}, and {C}make user-service{RESET} first.")
        sys.exit(1)

    pause()


async def stage_2_baseline(client: httpx.AsyncClient) -> None:
    header(2, "Baseline Health — Healthy State")

    data = await get(client, f"{USER_SVC}/metrics/raw")
    m = data.get("metrics", {})

    print(f"\n  {W}User-service v{data.get('version', '?')}{RESET}\n")

    metric_line("Requests/sec",          m.get("requests_per_sec", 0),    "",    True)
    metric_line("Redis hit rate",         m.get("cache_hit_rate_pct", 0),  "%",   m.get("cache_hit_rate_pct", 0) > 80)
    metric_line("DB queries/sec",         m.get("db_queries_per_sec", 0),  "",    m.get("db_queries_per_sec", 0) < 30)
    metric_line("P95 latency",            m.get("latency_p95_ms", 0),      "ms",  m.get("latency_p95_ms", 0) < 200)
    metric_line("Error rate",             m.get("error_rate_pct", 0),      "%",   m.get("error_rate_pct", 0) < 1)
    metric_line("DB pool used",           data.get("pool", {}).get("used", 0), "",  True)

    print(f"\n  {G}Service is healthy. Cache is warm. Database load is nominal.{RESET}")
    pause()


async def stage_3_warm_load(client: httpx.AsyncClient) -> None:
    header(3, "Simulating Traffic — Warming the Cache")

    print(f"  Sending {C}80 requests{RESET} to /users/{{id}}/profile …\n")
    ok_count, err_count = await send_load(client, n=80, concurrency=15)
    ok(f"{ok_count} successful  {err_count} errors")

    data = await get(client, f"{USER_SVC}/metrics/raw")
    m = data.get("metrics", {})
    hit_rate = m.get("cache_hit_rate_pct", 0)
    metric_line("Redis hit rate", hit_rate, "%", hit_rate > 80)
    metric_line("DB queries/sec", m.get("db_queries_per_sec", 0), "", True)
    metric_line("P95 latency",    m.get("latency_p95_ms", 0),      "ms", True)

    print(f"\n  {G}Cache is warm. ≥{hit_rate:.0f}% of requests served from Redis.{RESET}")
    pause()


async def stage_4_inject_bug(client: httpx.AsyncClient) -> None:
    header(4, "Inject Bug — Deployment v1.4.0")

    print(f"""
  {W}Simulating deployment v1.4.0 — "cache optimisation"{RESET}

  {DIM}The deployment changed the user-profile cache key:{RESET}

    {R}- cache_key = f"user:{{user_id}}:profile:v1"{RESET}
    {G}+ cache_key = f"user:{{user_id}}:profile:v2"{RESET}

  {DIM}Existing cached entries live under :v1 and will never be read again.{RESET}
    """)
    pause("Press ENTER to inject the deployment …")

    data = await post(
        client,
        f"{USER_SVC}/admin/failure",
        params={"mode": "cache_key_regression"},
    )
    ok(f"Failure mode set: {data}")
    print(f"\n  {Y}Bug injected. Service is now running v1.4.0.{RESET}")
    pause()


async def stage_5_observe_degradation(client: httpx.AsyncClient) -> None:
    header(5, "Observe Degradation — Cache Miss Cascade")

    print(f"  Sending traffic and watching metrics degrade …\n")

    # Send waves of load and print metrics after each
    for wave in range(1, 5):
        ok_count, err_count = await send_load(client, n=30, concurrency=10)
        data = await get(client, f"{USER_SVC}/metrics/raw")
        m = data.get("metrics", {})
        hit_rate = m.get("cache_hit_rate_pct", 0.0)
        db_qps   = m.get("db_queries_per_sec", 0.0)
        p95      = m.get("latency_p95_ms", 0.0)
        err_rate = m.get("error_rate_pct", 0.0)

        print(f"  {DIM}Wave {wave}{RESET}  "
              f"cache_hit={C}{hit_rate:.0f}%{RESET}  "
              f"db_qps={R}{db_qps:.0f}{RESET}  "
              f"p95={R}{p95:.0f}ms{RESET}  "
              f"err={R}{err_rate:.1f}%{RESET}")
        await asyncio.sleep(1.5)

    print(f"""
  {R}Redis hit rate collapsed.{RESET}
  Every API request is now hitting PostgreSQL directly.
  Database is saturating.
    """)
    pause()


async def stage_6_sentinel_detects(client: httpx.AsyncClient) -> dict:
    header(6, "Sentinel Detects the Anomaly")

    print(f"  Polling Sentinel for an open incident …\n")

    deadline = time.monotonic() + POLL_TIMEOUT
    incident: dict | None = None
    while time.monotonic() < deadline:
        incidents = await get(client, f"{SENTINEL}/api/v1/incidents")
        if incidents:
            incident = incidents[0]
            break
        await asyncio.sleep(POLL_INTERVAL)

    if not incident:
        # Manually push a metric to trigger detection
        info("No incident yet — pushing a metric snapshot to Sentinel …")
        data = await get(client, f"{USER_SVC}/metrics/raw")
        await post(
            client,
            f"{SENTINEL}/api/v1/metrics",
            json={
                "service": data.get("service", "user-service"),
                "version": data.get("version", "1.4.0"),
                "metrics": data.get("metrics", {}),
                "pool": data.get("pool", {}),
                "cache": data.get("cache", {}),
                "failure_mode": data.get("failure_mode", "cache_key_regression"),
            },
        )
        await asyncio.sleep(2)
        incidents = await get(client, f"{SENTINEL}/api/v1/incidents")
        if incidents:
            incident = incidents[0]

    if not incident:
        fail("Sentinel did not detect an incident. Check thresholds in sentinel/config.py.")
        sys.exit(1)

    ok(f"Incident opened: {W}{incident['id']}{RESET}")
    ok(f"Service: {incident['service']}  version: {incident.get('triggering_version', '?')}")
    ok(f"Severity: {R}{incident['severity']}{RESET}")
    ok(f"Status: {incident['status']}")

    pause()
    return incident


async def stage_7_sentinel_investigates(
    client: httpx.AsyncClient, incident: dict
) -> dict:
    header(7, "Sentinel Investigates — Causal Reasoning")

    print(f"  Waiting for ROOT_CAUSE_IDENTIFIED …\n")
    inc = await wait_for_incident_status(
        client,
        incident["id"],
        ["ROOT_CAUSE_IDENTIFIED", "REMEDIATION_CREATED", "PATCH_VALIDATED",
         "DEPLOYED", "VERIFICATION_PASSED", "RESOLVED"],
    )

    rca = inc.get("rca", {})
    if rca:
        print(f"\n  {W}Root Cause Analysis{RESET}\n")
        print(f"  {B}Root cause:{RESET}")
        # Word-wrap the root cause at 60 chars
        rc = rca.get("root_cause", "")
        words = rc.split()
        line, lines = [], []
        for w in words:
            if sum(len(x) + 1 for x in line) + len(w) > 60:
                lines.append(" ".join(line))
                line = [w]
            else:
                line.append(w)
        if line:
            lines.append(" ".join(line))
        for l in lines:
            print(f"    {l}")

        print(f"\n  {B}Confidence:{RESET} {G}{rca.get('confidence', 0) * 100:.0f}%{RESET}")

        chain = rca.get("reasoning_chain", [])
        if chain:
            print(f"\n  {B}Reasoning chain:{RESET}")
            for step in chain:
                print(f"    {DIM}{step}{RESET}")

        candidates = rca.get("candidate_causes", [])
        if candidates:
            print(f"\n  {B}Candidate causes (ranked):{RESET}")
            for i, c in enumerate(candidates, 1):
                colour = G if i == 1 else DIM
                print(
                    f"    {colour}{i}. {c['cause'][:70]}  "
                    f"({c['confidence_pct']:.0f}%){RESET}"
                )
    else:
        warn("RCA data not yet available")

    pause()
    return inc


async def stage_8_sentinel_remediates(
    client: httpx.AsyncClient, incident: dict
) -> dict:
    header(8, "Sentinel Creates Remediation")

    print(f"  Waiting for REMEDIATION_CREATED …\n")
    inc = await wait_for_incident_status(
        client,
        incident["id"],
        ["REMEDIATION_CREATED", "PATCH_VALIDATED", "DEPLOYED",
         "VERIFICATION_PASSED", "RESOLVED"],
    )

    print(f"\n  {W}Pull Request{RESET}\n")
    ok(f"Branch: {C}{inc.get('pr_branch', '?')}{RESET}")
    ok(f"URL:    {C}{inc.get('pr_url', '?')}{RESET}")

    print(f"\n  {W}Patch applied:{RESET}")
    print(f"""
    {R}- cache_key = f"user:{{user_id}}:profile:v2"{RESET}
    {G}+ cache_key = f"user:{{user_id}}:profile:v1"{RESET}

    {G}+ tests/test_cache_key_regression.py{RESET}
    """)

    pause()
    return inc


async def stage_9_sentinel_validates(
    client: httpx.AsyncClient, incident: dict
) -> dict:
    header(9, "Sentinel Validates the Patch")

    print(f"  Waiting for PATCH_VALIDATED …\n")
    inc = await wait_for_incident_status(
        client,
        incident["id"],
        ["PATCH_VALIDATED", "DEPLOYED", "VERIFICATION_PASSED", "RESOLVED"],
    )

    val = inc.get("validation", {})
    if val:
        print(f"\n  {W}Validation Results{RESET}\n")
        for check in val.get("checks", []):
            icon = f"{G}✅{RESET}" if check["passed"] else f"{R}❌{RESET}"
            print(f"  {icon}  {check['name']:<40} {DIM}{check['duration_s']:.1f}s{RESET}")
        print(f"\n  {val.get('summary', '').splitlines()[0]}")
    else:
        warn("Validation data not yet available")

    pause()
    return inc


async def stage_10_sentinel_verifies(
    client: httpx.AsyncClient, incident: dict
) -> None:
    header(10, "Sentinel Verifies — Before / After")

    print(f"  Running post-fix load test and comparing metrics …\n")
    inc = await wait_for_incident_status(
        client,
        incident["id"],
        ["RESOLVED", "FAILED"],
        timeout=POLL_TIMEOUT,
    )

    ver = inc.get("verification", {})
    if ver:
        before = ver.get("before", {})
        after  = ver.get("after", {})

        # Print comparison table
        print(f"\n  {W}{'METRIC':<30} {'BEFORE':>12} {'AFTER':>12} {'STATUS':>8}{RESET}")
        print(f"  {'─' * 66}")

        specs = [
            ("cache_hit_rate_pct", "%",   True),
            ("db_queries_per_sec", "q/s", False),
            ("latency_p95_ms",     "ms",  False),
            ("error_rate_pct",     "%",   False),
        ]
        for key, unit, higher_is_better in specs:
            b = before.get(key, 0.0)
            a = after.get(key, 0.0)
            improved = (a > b) if higher_is_better else (a < b)
            icon = f"{G}✅{RESET}" if improved else f"{R}❌{RESET}"
            print(
                f"  {key:<30} {R}{b:>10.1f}{unit}{RESET}  "
                f"{G}{a:>10.1f}{unit}{RESET}  {icon}"
            )

        passed = ver.get("passed", False)
        status = inc.get("status", "UNKNOWN")

        print()
        if passed and status == "RESOLVED":
            print(f"  {G}{BOLD}{'─' * 64}{RESET}")
            print(f"  {G}{BOLD}  {inc['id']} RESOLVED{RESET}")
            print(f"  {G}{BOLD}  Remediation verified. All resolution criteria met.{RESET}")
            print(f"  {G}{BOLD}{'─' * 64}{RESET}")
            print(f"""
  {W}Summary{RESET}
  ───────
  Primary failure:    Database saturation
  Underlying cause:   Cache-key regression in deployment v1.4.0
  Remediation:        Restored cache key `:v1` compatibility
  Verification:       {G}PASSED{RESET}
            """)
        else:
            print(f"  {R}Incident status: {status}{RESET}")
            summary = ver.get("summary", "")
            if summary:
                for line in summary.splitlines()[-5:]:
                    print(f"  {line}")
    else:
        warn("Verification data not yet available")
        info(f"Final status: {inc.get('status', 'UNKNOWN')}")
        if inc.get("timeline"):
            print(f"\n  {W}Timeline:{RESET}")
            for entry in inc["timeline"]:
                print(f"  {DIM}{entry}{RESET}")

    print(f"\n  {DIM}Dashboard: open dashboard/index.html in your browser{RESET}")
    print(f"  {DIM}Full detail: GET {SENTINEL}/api/v1/incidents/{incident['id']}{RESET}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"""
{B}{'═' * 64}{RESET}
{B}  Sentinel SRE Agent — Demo 2{RESET}
{B}  Redis Cache Stampede / Cascading Database Overload{RESET}
{B}{'═' * 64}{RESET}

  This demo walks through an autonomous incident lifecycle:

  {DIM}1.  Detect   — cache hit rate collapses after deployment v1.4.0
  2.  Investigate — Sentinel traces the cause to a cache-key mismatch
  3.  Remediate   — Sentinel patches user_service.py + adds a test
  4.  Validate    — ruff lint + pytest (including regression test)
  5.  Verify      — live load test confirms the fix worked{RESET}
    """)
    pause("Press ENTER to start …")

    async with httpx.AsyncClient() as client:
        await stage_1_env_check(client)
        await stage_2_baseline(client)
        await stage_3_warm_load(client)
        await stage_4_inject_bug(client)
        await stage_5_observe_degradation(client)
        incident = await stage_6_sentinel_detects(client)
        incident = await stage_7_sentinel_investigates(client, incident)
        incident = await stage_8_sentinel_remediates(client, incident)
        incident = await stage_9_sentinel_validates(client, incident)
        await stage_10_sentinel_verifies(client, incident)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n\n  {Y}Demo interrupted.{RESET}\n")
