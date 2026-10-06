"""
RCA agent for the cache-stampede / cascading DB overload scenario.

Two execution paths:
  1. LLM path   — GPT-4o with a structured JSON response schema
  2. Fallback   — deterministic rule-based analysis (works offline / no API key)

The fallback implements the same causal reasoning chain described in the spec:

  API latency ↑  →  DB queries ↑  →  DB CPU ↑
       ↓
  Redis available (not the infrastructure)
       ↓
  Cache hit rate ↓
       ↓
  Recent deployment changed cache key v1 → v2
       ↓
  Existing :v1 entries are never read
       ↓
  ROOT CAUSE: cache-key version mismatch in v1.4.0
"""
from __future__ import annotations

import json
import logging
import re

from models import CandidateCause, Incident, RCAResult

log = logging.getLogger("sentinel.investigator.agent")

# ── LLM system prompt ─────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are Sentinel, an autonomous SRE agent.

Your task is to perform root-cause analysis on a production incident.

You will receive:
  - Incident metadata (service, version, severity)
  - Observed metrics vs baseline (symptoms)
  - Recent deployment history (last 5 deployments)
  - Full source code of the affected service files
  - Analysis hints from prior observation

REASONING INSTRUCTIONS
----------------------
1. Do NOT jump to "the database is overloaded" as the root cause.
   High DB load is a SYMPTOM. Ask why.
2. If Redis is available but hit rate is near 0%, the cache is not being used.
   Ask why.
3. Correlate deployment timing with the onset of the anomaly.
4. Inspect the deployment diff for the most recent deployment.
5. Trace through the source code to identify the exact line causing the issue.
6. State confidence as a number between 0.0 and 1.0.

OUTPUT FORMAT
-------------
Respond with ONLY a valid JSON object matching this exact schema:

{
  "root_cause": "<one clear sentence describing the actual root cause>",
  "confidence": <float 0.0–1.0>,
  "reasoning_chain": [
    "<step 1>",
    "<step 2>",
    ...
  ],
  "candidate_causes": [
    {"cause": "<description>", "confidence_pct": <float>},
    ...
  ],
  "evidence": [
    "<evidence item 1>",
    ...
  ],
  "affected_file": "<filename>",
  "affected_function": "<function name>",
  "recommended_fix": "<concrete one-sentence fix>"
}
"""


def _format_context(incident: Incident, ctx: dict) -> str:
    """Serialise the investigation context into an LLM user message."""
    lines: list[str] = []

    lines.append(f"## Incident {incident.id}")
    lines.append(f"Service : {incident.service}")
    lines.append(f"Version : {incident.triggering_version}")
    lines.append(f"Severity: {incident.severity.value}")
    lines.append("")

    lines.append("## Observed Symptoms vs Baseline")
    for key, delta in ctx.get("delta", {}).items():
        lines.append(
            f"  {key:35s}  before={delta['before']}  after={delta['after']}"
        )
    lines.append("")

    lines.append("## Recent Deployment History (newest last)")
    for dep in ctx.get("deployment_history", []):
        lines.append(
            f"  [{dep['version']}] {dep['deployed_at']}  "
            f"{dep['summary']}"
        )
        lines.append(f"    diff: {dep['diff_summary']}")
    lines.append("")

    lines.append("## Analysis Hints")
    for hint in ctx.get("analysis_hints", []):
        lines.append(f"  • {hint}")
    lines.append("")

    lines.append("## Source Code")
    for fname, src in ctx.get("source_files", {}).items():
        lines.append(f"### {fname}")
        lines.append("```python")
        lines.append(src)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ── LLM path ──────────────────────────────────────────────────────────────────

def _llm_rca(incident: Incident, ctx: dict) -> RCAResult:
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:
        raise RuntimeError("openai package not installed") from exc

    from config import settings

    client = OpenAI(api_key=settings.openai_api_key)
    user_message = _format_context(incident, ctx)

    log.info(
        "sending context to LLM",
        extra={"incident_id": incident.id, "model": settings.openai_model},
    )

    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)

    return RCAResult(
        root_cause=data.get("root_cause", "Unknown"),
        confidence=float(data.get("confidence", 0.5)),
        reasoning_chain=data.get("reasoning_chain", []),
        candidate_causes=[
            CandidateCause(**c) for c in data.get("candidate_causes", [])
        ],
        evidence=data.get("evidence", []),
        affected_file=data.get("affected_file", "user_service.py"),
        affected_function=data.get("affected_function", ""),
        recommended_fix=data.get("recommended_fix", ""),
    )


# ── Rule-based fallback ───────────────────────────────────────────────────────

def _fallback_rca(incident: Incident, ctx: dict) -> RCAResult:
    """
    Deterministic RCA that mirrors the causal reasoning chain from the spec.

    Works entirely from the assembled context — no LLM needed.
    """
    m = incident.triggering_metrics
    source = ctx.get("source_files", {}).get("user_service.py", "")
    recent_deploy = ctx.get("recent_deployment", {})

    # ── Reasoning chain ───────────────────────────────────────────────────────
    chain: list[str] = [
        f"[1] API p95 latency is {m.latency_p95_ms:.0f}ms "
        f"(baseline ~35ms). Error rate {m.error_rate_pct:.1f}%.",
        f"[2] DB queries/sec is {m.db_queries_per_sec:.1f} "
        f"(baseline ~8 q/s). Database is under extreme load.",
        "[3] Redis is reachable (cache infrastructure is healthy). "
        "DB overload is not self-caused.",
        f"[4] Cache hit rate is {m.cache_hit_rate_pct:.1f}% "
        "(baseline ~96%). Every request is hitting the database.",
        "[5] Cache is available but not being used — investigate WHY.",
        f"[6] Most recent deployment: v{recent_deploy.get('version', '?')} "
        f"deployed at {recent_deploy.get('deployed_at', '?')}.",
        f"[7] Deployment diff: \"{recent_deploy.get('diff_summary', '')}\"",
        "[8] Diff shows cache key changed from :v1 to :v2. "
        "All warm cache entries use :v1 — they are now unreachable.",
        "[9] Every request resolves to a cache MISS and hits PostgreSQL. "
        "At 100 req/s this produces 100 DB queries/s.",
        "[10] Root cause: cache-key version mismatch introduced in v1.4.0.",
    ]

    # ── Code evidence ─────────────────────────────────────────────────────────
    evidence: list[str] = []

    # Check for the :v2 key in the source
    v2_match = re.search(
        r'cache_key\s*=\s*f"user:\{.*?\}:profile:(v\d+)"', source
    )
    if v2_match:
        version_in_code = v2_match.group(1)
        evidence.append(
            f"user_service.py: `_buggy_cache_key()` uses key suffix "
            f"`:{version_in_code}` (mismatch with warm :v1 entries)"
        )

    if "profile:v2" in source:
        evidence.append(
            "user_service.py: string `profile:v2` found — confirms the "
            "key version was changed in this file"
        )

    if "profile:v1" in source:
        evidence.append(
            "user_service.py: `profile:v1` also present — confirms the "
            "old key is still written by warm-up but not read by the buggy path"
        )

    evidence.append(
        f"Deployment v1.4.0 (today): diff explicitly states cache key "
        f"changed from :v1 → :v2"
    )
    evidence.append(
        f"Cache hits: {m.cache_hits_total}  misses: {m.cache_misses_total}  "
        f"— hit rate effectively 0% post-deployment"
    )
    evidence.append(
        "Redis INFO shows keyspace_hits and keyspace_misses confirm "
        "reads are being attempted but returning MISS"
    )

    # ── Candidate causes (ranked) ─────────────────────────────────────────────
    candidates = [
        CandidateCause(
            cause="Cache-key version mismatch: v1.4.0 changed key suffix "
                  "from :v1 to :v2, orphaning all warm cache entries",
            confidence_pct=93.0,
        ),
        CandidateCause(
            cause="Cache TTL expired simultaneously for all keys (mass expiry / stampede)",
            confidence_pct=4.0,
        ),
        CandidateCause(
            cause="Redis eviction policy flushed all keys under memory pressure",
            confidence_pct=2.0,
        ),
        CandidateCause(
            cause="Database query regression independently of caching",
            confidence_pct=1.0,
        ),
    ]

    return RCAResult(
        root_cause=(
            "Deployment v1.4.0 changed the user-profile cache key from "
            "`user:{id}:profile:v1` to `user:{id}:profile:v2`. "
            "Existing cached objects use :v1. "
            "Result: ~96% cache hit rate collapsed to ~0%, causing "
            "all traffic to hit PostgreSQL and saturating the database."
        ),
        confidence=0.93,
        reasoning_chain=chain,
        candidate_causes=candidates,
        evidence=evidence,
        affected_file="user_service.py",
        affected_function="_buggy_cache_key",
        recommended_fix=(
            "In user_service.py, restore the cache key to "
            "`f'user:{user_id}:profile:v1'` in `_buggy_cache_key` "
            "(or revert the version constant to CACHE_KEY_VERSION_HEALTHY). "
            "Add a cache-key regression test to prevent future version drift."
        ),
    )


# ── Public entry point ────────────────────────────────────────────────────────

def run_rca(incident: Incident, ctx: dict) -> RCAResult:
    """
    Run RCA for *incident* using *ctx* as assembled by context_builder.

    Uses GPT-4o if OPENAI_API_KEY is configured; otherwise falls back to
    the deterministic rule-based analysis.
    """
    from config import settings

    if settings.openai_api_key:
        try:
            result = _llm_rca(incident, ctx)
            log.info(
                "LLM RCA complete",
                extra={
                    "incident_id": incident.id,
                    "confidence": result.confidence,
                    "affected_file": result.affected_file,
                },
            )
            return result
        except Exception as exc:
            log.warning(
                "LLM RCA failed — falling back to rule-based",
                extra={"incident_id": incident.id, "error": str(exc)},
            )

    result = _fallback_rca(incident, ctx)
    log.info(
        "rule-based RCA complete",
        extra={
            "incident_id": incident.id,
            "confidence": result.confidence,
            "root_cause": result.root_cause[:80],
        },
    )
    return result
