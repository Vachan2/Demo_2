# Sentinel SRE Agent — Demo 2
## Redis Cache Stampede / Cascading Database Overload

Demonstrates Sentinel's **causal reasoning** across a distributed-systems
failure where the database is the *victim*, not the *cause*.

---

## The scenario

```
Users → FastAPI (user-service) → Redis cache → PostgreSQL
```

### Healthy state
| Metric | Value |
|---|---|
| Redis hit rate | ~96 % |
| DB queries/sec | ~8 |
| P95 latency | ~35 ms |
| Error rate | 0 % |

### After deployment v1.4.0 ("cache optimisation")

The deployment changed the cache key from `:v1` to `:v2`.
All warm cache entries live under `:v1` — they are never read again.

| Metric | Value |
|---|---|
| Redis hit rate | ~0 % |
| DB queries/sec | ~250–500 |
| P95 latency | ~3–7 s |
| Error rate | 5–30 % |

### Sentinel's conclusion

> Deployment v1.4.0 changed the user-profile cache key from
> `user:{id}:profile:v1` to `user:{id}:profile:v2`.
> Existing cached objects use :v1.
> Result: ~96% cache hit rate → ~0%, causing database saturation.
>
> **Confidence: 93%**

---

## Quick start

```bash
# 1. Start infrastructure
make services          # Postgres :5433 + Redis :6380 via docker-compose

# 2. Install dependencies
make setup

# 3. Start services (two separate terminals)
make user-service      # port 9001
make sentinel          # port 8002

# 4. Open the live dashboard
open dashboard/index.html

# 5. Run the interactive demo
make demo
```

---

## Architecture

```
demo/user-service/          Target microservice
  config.py                 Settings (failure mode toggle)
  database.py               asyncpg pool
  cache.py                  Redis client
  metrics.py                Rolling-window metrics store
  user_service.py           ← healthy (:v1) and buggy (:v2) implementations
  main.py                   FastAPI app + background metric pusher
  tests/
    test_user_service.py    Unit tests (cache key, routing, DB mock)
    test_api.py             API tests (FastAPI TestClient)
    test_integration.py     Integration tests (live Redis + Postgres)

sentinel/
  main.py                   FastAPI orchestrator + _pipeline() background task
  models.py                 Incident, RCAResult, ValidationResult, VerificationResult
  config.py                 Thresholds + API keys
  incident_store.py         Thread-safe in-memory store
  detector/monitor.py       Threshold evaluation → Incident creation
  investigator/
    context_builder.py      Assembles deployment history + source files
    agent.py                LLM (GPT-4o) or rule-based RCA
  github_tool/
    local_git.py            GitPython backend (offline)
    github_api.py           PyGitHub backend (real GitHub)
    remediation.py          Cache-key fix patch + regression test + PR
  validator/runner.py       ruff + pytest as subprocesses
  verifier/
    load_test.py            Async load generator
    engine.py               Before/after comparison + resolution

dashboard/index.html        Live metrics dashboard (no build step)
scripts/demo_runner.py      10-stage interactive demo
```

---

## Pipeline stages

```
DETECTING → INVESTIGATING → ROOT_CAUSE_IDENTIFIED
→ REMEDIATION_CREATED → PATCH_VALIDATED → DEPLOYED
→ VERIFICATION_PASSED → RESOLVED
```

---

## Configuration

Copy `sentinel/.env.example` to `sentinel/.env` and optionally set:

```bash
OPENAI_API_KEY=sk-...          # enables GPT-4o RCA (optional)
GITHUB_TOKEN=ghp_...           # enables real GitHub PRs (optional)
GITHUB_REPO=your-org/demo2
```

Without these the system uses rule-based RCA and local-git fallback.

---

## Comparison with Demo 1

| Capability | Demo 1 (DB connection leak) | Demo 2 (Cache stampede) |
|---|---|---|
| Detect anomaly | ✓ | ✓ |
| Analyze logs | ✓ | ✓ |
| Analyze metrics | ✓ | ✓ |
| Inspect recent deployment | ✓ | ✓ |
| Inspect source code | ✓ | ✓ |
| Understand dependencies | Moderate | **Strong** |
| Causal reasoning | Strong | **Very strong** |
| Code remediation | ✓ | ✓ |
| Automated tests | ✓ | ✓ |
| GitHub PR | ✓ | ✓ |
| Before/after verification | ✓ | ✓ |
