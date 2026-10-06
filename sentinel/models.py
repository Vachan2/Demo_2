"""
Sentinel data models for Demo 2 (cache stampede / DB overload).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enumerations ──────────────────────────────────────────────────────────────

class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(str, Enum):
    DETECTING             = "DETECTING"
    INVESTIGATING         = "INVESTIGATING"
    ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED"
    REMEDIATION_CREATED   = "REMEDIATION_CREATED"
    PATCH_VALIDATED       = "PATCH_VALIDATED"
    DEPLOYED              = "DEPLOYED"
    VERIFICATION_PASSED   = "VERIFICATION_PASSED"
    RESOLVED              = "RESOLVED"
    FAILED                = "FAILED"


# ── Inbound payload from user-service ────────────────────────────────────────

class CacheMetrics(BaseModel):
    available: bool = True
    keyspace_hits: int = 0
    keyspace_misses: int = 0
    connected_clients: int = 0
    used_memory_mb: float = 0.0


class PoolMetrics(BaseModel):
    size: int = 0
    idle: int = 0
    used: int = 0
    max: int = 20


class ServiceMetrics(BaseModel):
    requests_per_sec: float = 0.0
    error_rate_pct: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_avg_ms: float = 0.0
    cache_hit_rate_pct: float = 0.0
    cache_hits_total: int = 0
    cache_misses_total: int = 0
    db_queries_per_sec: float = 0.0
    db_query_latency_avg_ms: float = 0.0


class MetricsPayload(BaseModel):
    service: str
    version: str
    metrics: ServiceMetrics
    pool: PoolMetrics = Field(default_factory=PoolMetrics)
    cache: CacheMetrics = Field(default_factory=CacheMetrics)
    failure_mode: str = "none"


# ── Incident ──────────────────────────────────────────────────────────────────

class Incident(BaseModel):
    id: str = Field(default_factory=lambda: f"INC-{str(uuid.uuid4())[:8].upper()}")
    service: str
    severity: Severity = Severity.HIGH
    status: IncidentStatus = IncidentStatus.DETECTING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Snapshot of metrics that triggered this incident
    triggering_metrics: ServiceMetrics = Field(default_factory=ServiceMetrics)
    triggering_version: str = ""
    triggering_failure_mode: str = ""

    # Populated by each pipeline stage
    rca: "RCAResult | None" = None
    pr_url: str = ""
    pr_branch: str = ""
    validation: "ValidationResult | None" = None
    verification: "VerificationResult | None" = None

    # Human-readable timeline of status transitions
    timeline: list[str] = Field(default_factory=list)

    def transition(self, new_status: IncidentStatus, note: str = "") -> None:
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc)
        entry = f"[{self.updated_at.strftime('%H:%M:%S')}] → {new_status.value}"
        if note:
            entry += f" — {note}"
        self.timeline.append(entry)


# ── RCA ───────────────────────────────────────────────────────────────────────

class CandidateCause(BaseModel):
    cause: str
    confidence_pct: float


class RCAResult(BaseModel):
    root_cause: str
    confidence: float                           # 0.0–1.0
    candidate_causes: list[CandidateCause] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    affected_file: str = ""
    affected_function: str = ""
    recommended_fix: str = ""
    reasoning_chain: list[str] = Field(default_factory=list)


# ── Validation ────────────────────────────────────────────────────────────────

class CheckResult(BaseModel):
    name: str
    passed: bool
    output: str = ""
    duration_s: float = 0.0


class ValidationResult(BaseModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    summary: str = ""


# ── Verification ──────────────────────────────────────────────────────────────

class MetricComparison(BaseModel):
    metric: str
    before: Any
    after: Any
    improved: bool
    unit: str = ""


class VerificationResult(BaseModel):
    passed: bool
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    comparisons: list[MetricComparison] = Field(default_factory=list)
    summary: str = ""


# ── PR ────────────────────────────────────────────────────────────────────────

class PRResult(BaseModel):
    url: str
    branch: str
    title: str
    files_changed: list[str] = Field(default_factory=list)
