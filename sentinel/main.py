"""
Sentinel SRE Agent — Demo 2
============================
FastAPI application that receives metrics from the user-service, detects
anomalies, and autonomously runs a four-stage remediation pipeline:

  DETECTING → INVESTIGATING → ROOT_CAUSE_IDENTIFIED
  → REMEDIATION_CREATED → PATCH_VALIDATED → DEPLOYED
  → VERIFICATION_PASSED → RESOLVED

All pipeline stages run as a FastAPI BackgroundTask so the /metrics
endpoint returns immediately while investigation proceeds.

API
---
POST /api/v1/metrics            ← receive metric push from user-service
GET  /api/v1/incidents          ← list all incidents
GET  /api/v1/incidents/{id}     ← get single incident detail
POST /api/v1/incidents/{id}/run ← manually trigger pipeline (for demos)
GET  /health                    ← liveness probe
GET  /api/v1/status             ← Sentinel status + open incident count
"""
from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pythonjsonlogger import jsonlogger

from config import settings
from detector.monitor import evaluate
from github_tool.remediation import create_fix
from incident_store import all_incidents, get, save
from investigator.agent import run_rca
from investigator.context_builder import build as build_context
from models import Incident, IncidentStatus, MetricsPayload
from validator.runner import run_all as run_validation
from verifier.engine import verify

# ── Logging ───────────────────────────────────────────────────────────────────
_handler = logging.StreamHandler()
_handler.setFormatter(
    jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
)
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("sentinel")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Sentinel SRE Agent",
    version=settings.sentinel_version,
    description="Autonomous root-cause analysis and remediation — Demo 2",
)


# ─────────────────────────────────────────────────────────────────────────────
# Core pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def _pipeline(incident: Incident) -> None:
    """
    Run the full autonomous investigation → remediation → validation →
    verification pipeline for *incident*.

    Each stage:
      1. Transitions the incident status
      2. Persists the updated incident
      3. Logs progress

    On any unhandled exception the incident is marked FAILED.
    """
    try:
        # ── Stage 1: Investigate ──────────────────────────────────────────────
        incident.transition(
            IncidentStatus.INVESTIGATING,
            "building investigation context",
        )
        save(incident)
        log.info("pipeline: investigating", extra={"incident_id": incident.id})

        ctx = build_context(incident)
        rca = run_rca(incident, ctx)
        incident.rca = rca

        incident.transition(
            IncidentStatus.ROOT_CAUSE_IDENTIFIED,
            f"confidence={rca.confidence * 100:.0f}% "
            f"file={rca.affected_file} "
            f"func={rca.affected_function}",
        )
        save(incident)
        log.info(
            "pipeline: RCA complete",
            extra={
                "incident_id": incident.id,
                "confidence": rca.confidence,
                "root_cause": rca.root_cause[:80],
            },
        )

        # ── Stage 2: Remediate ────────────────────────────────────────────────
        pr = create_fix(incident, rca)
        incident.pr_url = pr.url
        incident.pr_branch = pr.branch

        incident.transition(
            IncidentStatus.REMEDIATION_CREATED,
            f"branch={pr.branch} files={pr.files_changed}",
        )
        save(incident)
        log.info(
            "pipeline: remediation created",
            extra={
                "incident_id": incident.id,
                "pr_url": pr.url,
                "branch": pr.branch,
            },
        )

        # ── Stage 3: Validate ─────────────────────────────────────────────────
        # The validator runs tests against the PATCHED files that were written
        # by the remediation step (either into the local repo or GitHub).
        # For the local-git backend the working tree has already been updated.
        validation = await run_validation(incident)
        incident.validation = validation

        if not validation.passed:
            incident.transition(
                IncidentStatus.FAILED,
                f"validation failed: {validation.summary[:120]}",
            )
            save(incident)
            log.error(
                "pipeline: validation FAILED",
                extra={"incident_id": incident.id, "summary": validation.summary},
            )
            return

        incident.transition(
            IncidentStatus.PATCH_VALIDATED,
            f"all checks passed — {validation.summary.splitlines()[0]}",
        )
        incident.transition(
            IncidentStatus.DEPLOYED,
            "patch applied to running service (hot reload via uvicorn --reload)",
        )
        save(incident)
        log.info(
            "pipeline: validation passed",
            extra={"incident_id": incident.id},
        )

        # ── Stage 4: Verify ───────────────────────────────────────────────────
        # Run a live load test against the patched service and compare
        # before vs. after metrics.
        verification = await verify(incident)
        # verify() handles the status transitions (RESOLVED or FAILED) itself.

        log.info(
            "pipeline: verification complete",
            extra={
                "incident_id": incident.id,
                "resolved": verification.passed,
                "summary": verification.summary.splitlines()[0]
                if verification.summary
                else "",
            },
        )

    except Exception as exc:
        log.exception(
            "pipeline: unhandled error",
            extra={"incident_id": incident.id, "error": str(exc)},
        )
        incident.transition(
            IncidentStatus.FAILED,
            f"unhandled pipeline error: {type(exc).__name__}: {exc}",
        )
        save(incident)


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/v1/metrics", status_code=202)
async def receive_metrics(
    payload: MetricsPayload,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Receive a metric push from the user-service.

    If any threshold is breached and no open incident exists, open a new
    incident and start the autonomous pipeline in the background.
    """
    incident = evaluate(payload)

    if incident:
        log.warning(
            "anomaly detected — starting pipeline",
            extra={
                "incident_id": incident.id,
                "service": incident.service,
                "version": payload.version,
            },
        )
        background_tasks.add_task(_pipeline, incident)
        return {
            "anomaly_detected": True,
            "incident_id": incident.id,
            "status": incident.status.value,
        }

    return {"anomaly_detected": False}


@app.get("/api/v1/incidents")
async def list_incidents() -> list[dict]:
    """Return all incidents, newest first."""
    incidents = sorted(
        all_incidents(),
        key=lambda inc: inc.created_at,
        reverse=True,
    )
    return [_incident_summary(inc) for inc in incidents]


@app.get("/api/v1/incidents/{incident_id}")
async def get_incident(incident_id: str) -> dict:
    """Return full detail for a single incident."""
    inc = get(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    return _incident_detail(inc)


@app.post("/api/v1/incidents/{incident_id}/run", status_code=202)
async def run_incident_pipeline(
    incident_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Manually trigger the pipeline for an existing incident.
    Useful for re-running after configuration changes or in live demos.
    """
    inc = get(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

    terminal = {IncidentStatus.RESOLVED, IncidentStatus.FAILED}
    if inc.status in terminal:
        raise HTTPException(
            status_code=409,
            detail=f"Incident {incident_id} is already in terminal state {inc.status.value}",
        )

    background_tasks.add_task(_pipeline, inc)
    return {"triggered": True, "incident_id": incident_id, "status": inc.status.value}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "sentinel",
        "version": settings.sentinel_version,
    }


@app.get("/api/v1/status")
async def status() -> dict:
    all_inc = all_incidents()
    terminal = {IncidentStatus.RESOLVED, IncidentStatus.FAILED}
    open_count = sum(1 for i in all_inc if i.status not in terminal)
    return {
        "sentinel_version": settings.sentinel_version,
        "total_incidents": len(all_inc),
        "open_incidents": open_count,
        "thresholds": {
            "cache_hit_rate_warn_pct": settings.cache_hit_rate_warn_pct,
            "db_queries_per_sec_warn": settings.db_queries_per_sec_warn,
            "latency_p95_warn_ms": settings.latency_p95_warn_ms,
            "error_rate_warn_pct": settings.error_rate_warn_pct,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Response helpers
# ─────────────────────────────────────────────────────────────────────────────

def _incident_summary(inc: Incident) -> dict:
    return {
        "id": inc.id,
        "service": inc.service,
        "severity": inc.severity.value,
        "status": inc.status.value,
        "created_at": inc.created_at.isoformat(),
        "updated_at": inc.updated_at.isoformat(),
        "triggering_version": inc.triggering_version,
        "pr_url": inc.pr_url,
    }


def _incident_detail(inc: Incident) -> dict:
    d = _incident_summary(inc)
    d["triggering_metrics"] = inc.triggering_metrics.model_dump()
    d["timeline"] = inc.timeline
    if inc.rca:
        d["rca"] = {
            "root_cause": inc.rca.root_cause,
            "confidence": inc.rca.confidence,
            "confidence_pct": round(inc.rca.confidence * 100, 0),
            "reasoning_chain": inc.rca.reasoning_chain,
            "candidate_causes": [
                c.model_dump() for c in inc.rca.candidate_causes
            ],
            "evidence": inc.rca.evidence,
            "affected_file": inc.rca.affected_file,
            "affected_function": inc.rca.affected_function,
            "recommended_fix": inc.rca.recommended_fix,
        }
    if inc.validation:
        d["validation"] = {
            "passed": inc.validation.passed,
            "summary": inc.validation.summary,
            "checks": [c.model_dump() for c in inc.validation.checks],
        }
    if inc.verification:
        d["verification"] = {
            "passed": inc.verification.passed,
            "summary": inc.verification.summary,
            "before": inc.verification.before,
            "after": inc.verification.after,
            "comparisons": [c.model_dump() for c in inc.verification.comparisons],
        }
    return d
