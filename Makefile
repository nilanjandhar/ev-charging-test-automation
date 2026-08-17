.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON  ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin/
PYTEST  := $(BIN)pytest
BASE_URL ?= http://localhost:8000
COMPOSE ?= docker compose -f service/docker-compose.yml

# The PR gate: everything that is fast, hermetic and gates a merge. `slow` is
# excluded by marker rather than by path, so a slow test added to a fast layer
# still drops out of the gate automatically.
GATE_MARKERS := not e2e and not perf and not ui and not slow

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
.PHONY: install
install:  ## Create the venv and install test + service dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)pip install --upgrade pip
	$(BIN)pip install -r requirements-dev.txt

.PHONY: install-ui
install-ui: install  ## Additionally install Playwright and its browser (~150MB)
	$(BIN)pip install -r requirements-ui.txt
	$(BIN)playwright install --with-deps chromium

# --------------------------------------------------------------------------
# Test layers
# --------------------------------------------------------------------------
.PHONY: test
test:  ## The PR gate: unit + contract + api, no service required (~3s)
	$(PYTEST) -m "$(GATE_MARKERS)"

.PHONY: test-unit
test-unit:  ## Scoring boundaries and Hypothesis properties
	$(PYTEST) -m unit

.PHONY: test-contract
test-contract:  ## OpenAPI conformance (fast) — excludes the schemathesis fuzz suite
	$(PYTEST) -m "contract and not slow"

.PHONY: test-fuzz
test-fuzz:  ## Schemathesis, bounded by SCHEMATHESIS_MAX_EXAMPLES (default 15)
	$(PYTEST) -m "contract and slow"

.PHONY: test-api
test-api:  ## In-process integration against an isolated database
	$(PYTEST) -m api

.PHONY: test-e2e
test-e2e:  ## Live HTTP against BASE_URL; skips with a message if nothing is listening
	BASE_URL=$(BASE_URL) $(PYTEST) -m "e2e and not perf"

.PHONY: test-perf
test-perf:  ## Latency + concurrency smoke. Never gates a merge. Prints its numbers
	BASE_URL=$(BASE_URL) $(PYTEST) -m perf -s

.PHONY: test-ui
test-ui:  ## One Playwright dashboard smoke test (needs `make install-ui`)
	BASE_URL=$(BASE_URL) $(PYTEST) -m ui

.PHONY: test-all
test-all:  ## Everything, including layers that need a running service
	BASE_URL=$(BASE_URL) $(PYTEST) -s

.PHONY: coverage
coverage:  ## Gate layers with coverage of the service, reported and written to XML
	$(PYTEST) -m "$(GATE_MARKERS)" \
		--cov --cov-report=term-missing --cov-report=xml:reports/coverage.xml \
		--junitxml=reports/junit.xml

# --------------------------------------------------------------------------
# Quality
# --------------------------------------------------------------------------
.PHONY: lint
lint:  ## ruff check + format check
	$(BIN)ruff check tests
	$(BIN)ruff format --check tests

.PHONY: format
format:  ## Apply ruff's formatter and autofixes
	$(BIN)ruff check --fix tests
	$(BIN)ruff format tests

.PHONY: typecheck
typecheck:  ## mypy --strict over the test suite
	$(BIN)mypy

.PHONY: check
check: lint typecheck test  ## Everything the PR gate runs, locally

# --------------------------------------------------------------------------
# Running the service under test
# --------------------------------------------------------------------------
.PHONY: run-service
run-service:  ## Run the service locally on :8000 (SQLite, no simulated latency)
	@echo "note: no --reload. It adds a file watcher and jitter that make perf numbers meaningless."
	cd service && DATABASE_URL=sqlite:///./noc.db ../$(BIN)uvicorn app.main:app --port 8000

.PHONY: docker-up
docker-up:  ## Start the containerised service (PostgreSQL + 40ms simulated latency)
	$(COMPOSE) up -d --build
	@echo "waiting for readiness — compose gives the api container no healthcheck"
	@BASE_URL=$(BASE_URL) $(BIN)python -c "import sys; \
		from tests.helpers.clients import probe_service; \
		reason = probe_service('$(BASE_URL)', 90.0); \
		sys.exit(reason or 0)"

.PHONY: docker-down
docker-down:  ## Stop it and drop the volume (state survives `down` without -v)
	$(COMPOSE) down -v

.PHONY: clean
clean:  ## Remove caches, reports and stray databases
	rm -rf .pytest_cache .hypothesis .mypy_cache .ruff_cache reports htmlcov .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -f service/*.db
