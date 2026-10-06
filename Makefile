.PHONY: help setup db redis services sentinel user-service stop-all test demo clean

PYTHON   := python3
PIP      := $(PYTHON) -m pip
UV       := uv

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  := \033[0;32m
YELLOW := \033[0;33m
CYAN   := \033[0;36m
RESET  := \033[0m

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "} {printf "$(CYAN)%-22s$(RESET) %s\n", $$1, $$2}'

# ── Setup ─────────────────────────────────────────────────────────────────────
setup: ## Install all Python dependencies
	@echo "$(GREEN)Installing user-service deps…$(RESET)"
	cd demo/user-service && $(PIP) install -r requirements.txt
	@echo "$(GREEN)Installing Sentinel deps…$(RESET)"
	cd sentinel && $(PIP) install -r requirements.txt
	@echo "$(GREEN)Setup complete.$(RESET)"

# ── Infrastructure ────────────────────────────────────────────────────────────
db: ## Start PostgreSQL container (standalone)
	docker run --rm -d \
	  --name demo2-postgres \
	  -e POSTGRES_DB=userdb \
	  -e POSTGRES_USER=demo \
	  -e POSTGRES_PASSWORD=demo \
	  -p 5433:5432 \
	  postgres:16-alpine

redis: ## Start Redis container (standalone)
	docker run --rm -d \
	  --name demo2-redis \
	  -p 6380:6379 \
	  redis:7-alpine

services: ## Start both PostgreSQL and Redis via docker-compose
	docker compose -f demo/user-service/docker-compose.yml up -d

seed: ## Seed the database
	@sleep 2
	PGPASSWORD=demo psql -h 127.0.0.1 -p 5433 -U demo -d userdb -f demo/user-service/seed.sql

# ── Running ───────────────────────────────────────────────────────────────────
user-service: ## Start the user-service API on port 9001
	cd demo/user-service && \
	  SENTINEL_URL=http://localhost:8002 \
	  DATABASE_URL="postgresql://demo:demo@localhost:5433/userdb" \
	  REDIS_URL="redis://localhost:6380" \
	  uvicorn main:app --host 0.0.0.0 --port 9001 --reload

sentinel: ## Start Sentinel on port 8002
	cd sentinel && \
	  ORDER_API_URL=http://localhost:9001 \
	  LOCAL_REPO_PATH=../demo/user-service \
	  uvicorn main:app --host 0.0.0.0 --port 8002 --reload

stop-all: ## Stop all Docker containers for this demo
	docker stop demo2-postgres demo2-redis 2>/dev/null || true
	docker compose -f demo/user-service/docker-compose.yml down 2>/dev/null || true

# ── Testing ───────────────────────────────────────────────────────────────────
test: ## Run user-service unit tests
	cd demo/user-service && pytest tests/ -v --tb=short

test-sentinel: ## Run Sentinel unit tests
	cd sentinel && pytest tests/ -v --tb=short 2>/dev/null || echo "No sentinel tests yet"

lint: ## Lint all Python files
	ruff check demo/user-service sentinel scripts

# ── Demo ──────────────────────────────────────────────────────────────────────
demo: ## Run the interactive demo script
	$(PYTHON) scripts/demo_runner.py

inject-bug: ## Inject the cache-key regression into the running user-service
	curl -s -X POST "http://localhost:9001/admin/failure?mode=cache_key_regression" | python3 -m json.tool

restore: ## Restore healthy mode
	curl -s -X POST "http://localhost:9001/admin/failure?mode=none" | python3 -m json.tool

status: ## Show current service status
	@echo "$(CYAN)User-service health:$(RESET)"
	@curl -s http://localhost:9001/health | python3 -m json.tool 2>/dev/null || echo "  not running"
	@echo "$(CYAN)Sentinel health:$(RESET)"
	@curl -s http://localhost:8002/health | python3 -m json.tool 2>/dev/null || echo "  not running"

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean: ## Remove caches and bytecode
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
