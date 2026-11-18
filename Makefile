# NOC Agent developer shortcuts. Each target is a thin wrapper over the same command CI runs
# (.github/workflows/ci.yml), so `make check` locally means the same as a green CI job.
#
# Windows: use a shell that provides sh (Git Bash) or run `make PY="py -3.12"` if `python` is not
# on PATH. Targets never touch production: tests use in-memory SQLite and fixture data.

PY ?= python
NPM ?= npm
COMPOSE ?= docker compose

ifeq ($(OS),Windows_NT)
NULL_FILE := NUL
else
NULL_FILE := /dev/null
endif

# Keep tests independent of any real .env and any real database.
export NOC_AGENT_ENV_FILE ?= $(NULL_FILE)
export DATABASE_URL ?= sqlite+aiosqlite:///:memory:

REPORT_DIR ?= .check-results/evals

.DEFAULT_GOAL := help

.PHONY: help install lint test eval check \
	lint-frontend typecheck test-frontend build-frontend e2e check-frontend \
	up down logs ps migrate clean

help: ## Show this list
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

# --- Backend ---------------------------------------------------------------------------------

install: ## Install backend (dev) and frontend dependencies
	$(PY) -m pip install -r backend/requirements-dev.txt
	cd frontend && $(NPM) ci --no-audit --no-fund

lint: ## Ruff over the backend
	$(PY) -m ruff check backend

test: ## Backend unit tests (PostgreSQL/Docker/SSH tests skip without their env vars)
	$(PY) -m unittest discover -s backend/tests

eval: ## Offline contract, adversarial and workflow evals (no network, no API keys)
	cd backend && $(PY) -m evals.runner --kind contracts
	cd backend && $(PY) -m evals.adversarial
	cd backend && $(PY) -m evals.runner --kind benchmark --quality-evaluator none --results-dir ../$(REPORT_DIR)

check: lint test eval ## Everything the backend CI job runs, minus the Docker-only parts

# --- Frontend --------------------------------------------------------------------------------

lint-frontend: ## eslint (zero warnings)
	cd frontend && $(NPM) run lint

typecheck: ## TypeScript type check
	cd frontend && $(NPM) run typecheck

test-frontend: ## Frontend unit tests
	cd frontend && $(NPM) test

build-frontend: ## Production build
	cd frontend && $(NPM) run build

e2e: ## Playwright browser tests against fixtures (needs `npx playwright install chromium` once)
	cd frontend && $(NPM) run test:e2e

check-frontend: lint-frontend typecheck test-frontend build-frontend e2e ## Everything the frontend CI job runs

# --- Local stack (fixture data, external worker, PostgreSQL control database) ----------------
# .env.example sets DATA_MODE=live and --env-file feeds compose interpolation, so without an
# explicit override the "development" stack would try real data sources with fake credentials.
# A shell variable wins over --env-file, so pin fixture data here. Override: make up STACK_DATA_MODE=live
STACK_DATA_MODE ?= fixture
WEB_PORT ?= 8080
STACK := DATA_MODE=$(STACK_DATA_MODE) NOC_WEB_PORT=$(WEB_PORT) $(COMPOSE) --env-file .env.example

up: ## Build and start the fixture stack on http://localhost:8080 (WEB_PORT=... to change)
	$(STACK) up --build --detach

down: ## Stop the development stack (keeps the database volume)
	$(STACK) down

logs: ## Follow stack logs
	$(STACK) logs -f

ps: ## Show stack status
	$(STACK) ps

migrate: ## Run control database migrations (the `migrate` compose service)
	$(STACK) run --rm migrate

clean: ## Remove lint/browser caches only; .check-results (saved run logs) is left alone
	rm -rf .ruff_cache backend/.ruff_cache frontend/playwright-report frontend/test-results
