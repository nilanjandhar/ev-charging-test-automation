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

# --------------------------------------------------------------------------
# Priority tiers — P0/P1/P2, derived from the risk register. See TEST_STRATEGY.md.
# Orthogonal to the layer markers, so they compose: `-m "p0 and api"` works.
# --------------------------------------------------------------------------
.PHONY: test-p0
test-p0:  ## P0 only: the service is doing its core job wrong. The fastest useful signal
	$(PYTEST) -m "p0 and ($(GATE_MARKERS))"

.PHONY: test-p1
test-p1:  ## P1 only: real defects with a narrower blast radius
	$(PYTEST) -m "p1 and ($(GATE_MARKERS))"

.PHONY: test-p2
test-p2:  ## P2 only: worth having, not worth blocking on
	$(PYTEST) -m "p2 and ($(GATE_MARKERS))"

.PHONY: smoke
smoke:  ## Alias for test-p0 — what to run when you have seconds, not minutes
	@$(MAKE) --no-print-directory test-p0

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
	# The UI layer runs in its own pytest process, deliberately. Playwright's sync
	# API drives a greenlet-backed event loop, and if the browser fails to launch
	# — the common case being `pip install -r requirements-ui.txt` without
	# `playwright install chromium` — the loop is left in a state that makes
	# *unrelated* asyncio and TestClient tests fail with
	# "Runner.run() cannot be called from a running event loop". Observed while
	# building this suite. Splitting the process bounds the blast radius to the
	# layer that actually broke, and it mirrors CI, where the UI suite is its own
	# job. `|| true` is not used: a real UI failure must still fail the target.
	BASE_URL=$(BASE_URL) $(PYTEST) -s -m "not ui"
	BASE_URL=$(BASE_URL) $(PYTEST) -m ui

.PHONY: coverage
coverage:  ## Gate layers with coverage of the service, reported and written to XML
	$(PYTEST) -m "$(GATE_MARKERS)" \
		--cov --cov-report=term-missing --cov-report=xml:reports/coverage.xml \
		--junitxml=reports/junit.xml

# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------
.PHONY: inventory
inventory:  ## Regenerate notes/test-inventory.md from the test sources
	$(BIN)python tools/test_inventory.py

.PHONY: report
report:  ## Build reports/test-report.html from whatever JUnit XML is in reports/
	$(BIN)python tools/test_report.py

.PHONY: test-report
test-report:  ## Run the gate, then build the HTML report — works whether it passed or not
	# The leading `-` matters: pytest must be allowed to fail here, because a run
	# with failures is exactly the run you want a report for. Without it, make
	# would abort before the report was written and the reader would be left with
	# terminal scrollback — which is the problem the report exists to solve.
	-$(PYTEST) -m "$(GATE_MARKERS)" --junitxml=reports/junit.xml
	$(BIN)python tools/test_report.py
	@echo "open reports/test-report.html"

# --------------------------------------------------------------------------
# Quality
# --------------------------------------------------------------------------
.PHONY: lint
lint:  ## ruff check + format check
	$(BIN)ruff check tests tools
	$(BIN)ruff format --check tests tools

.PHONY: format
format:  ## Apply ruff's formatter and autofixes
	$(BIN)ruff check --fix tests tools
	$(BIN)ruff format tests tools

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
