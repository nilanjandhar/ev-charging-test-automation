# Station Health API — test automation suite

[![PR gate](https://github.com/OWNER/REPO/actions/workflows/pr.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/pr.yml)
[![main](https://github.com/OWNER/REPO/actions/workflows/main.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/main.yml)
[![nightly](https://github.com/OWNER/REPO/actions/workflows/nightly.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/nightly.yml)

> Replace `OWNER/REPO` in the three badge URLs above with your GitHub path once
> the repository is pushed.

A test suite for a FastAPI microservice that ingests EV charging station health
reports, computes a network hygiene score, and exposes REST endpoints for station
status and network metrics. The service is in [`service/`](service/) and is
**never modified** by this suite.

- **[TEST_STRATEGY.md](TEST_STRATEGY.md)** — what I chose to test and why, the
  isolation decision, CI gating rationale, what I deliberately left out, and the
  eight service defects found.
- **[AI_USAGE.md](AI_USAGE.md)** — how I used an AI assistant: one output accepted,
  one substantially rewritten, one rejected.
- **[notes/](notes/)** — the working artifacts behind the strategy: behaviour
  inventory, risk register, Docker-vs-local analysis, mutation-check output, and
  the raw AI journal.

## Quick start

```bash
make install     # venv + test and service dependencies
make test        # the merge gate: unit + contract + api. ~2s, no service needed
```

That is the whole setup on a clean clone. The fast layers run the service
in-process, so nothing has to be listening on a port.

Requires Python 3.11+. Docker is needed only for the e2e and UI layers.

## Running each layer

| Command | What it runs | Needs a running service? |
|---|---|---|
| `make test` | The merge gate: unit + contract + api | No |
| `make test-unit` | Scoring boundaries + Hypothesis properties | No |
| `make test-api` | In-process integration, isolated database per test | No |
| `make test-contract` | OpenAPI conformance against `/openapi.json` | No |
| `make test-fuzz` | schemathesis, bounded by `SCHEMATHESIS_MAX_EXAMPLES` | No |
| `make test-e2e` | Live HTTP: real ASGI server, real serialisation | **Yes** |
| `make test-perf` | Latency + concurrency smoke; prints its numbers | **Yes** |
| `make test-ui` | One Playwright dashboard smoke (`make install-ui` first) | **Yes** |
| `make test-all` | Everything | Yes (others skip cleanly) |
| `make check` | `lint` + `typecheck` + `test` — what CI gates on | No |
| `make test-report` | The gate, then an HTML report of the results | No |

## Priority tiers

Every test declares exactly one of **P0**, **P1** or **P2**, orthogonal to its
layer. The tier answers "which red test do I read first?" and is derived from the
risk register — full reasoning in
[TEST_STRATEGY.md](TEST_STRATEGY.md#priority-tiers-p0--p1--p2) and per-test in
[notes/risk-register.md](notes/risk-register.md#priority-tiers).

| Tier | Means | Tests |
|---|---|---|
| **P0** | The service is doing its core job wrong: the score, the flag decision, which report counts as latest, or the endpoints disagreeing about one station. Stop and fix. | 42 |
| **P1** | A real defect with a narrower blast radius, or a specific edge. Fix before release. | 53 |
| **P2** | Worth having, not worth blocking on. File it. | 14 |

```bash
make smoke        # P0 only — 37 tests, ~1s. What to run when you have seconds
make test-p0      # same thing, spelled out
make test-p1
make test-p2
pytest -m "p0 and api"        # tiers compose with layer markers
```

`tests/conftest.py` **refuses to collect a test that declares no tier** (or that
declares two), so a new test cannot slip in untriaged:

```
ERROR: these tests declare no priority tier — add @pytest.mark.p0/p1/p2
  tests/unit/test_untiered_probe.py::test_someone_forgot_to_triage_me
```

## The HTML report

```bash
make test-report        # runs the gate, writes reports/test-report.html
make report             # rebuild from whatever JUnit XML is already in reports/
open reports/test-report.html
```

A single self-contained page (~340 lines of Python, no extra plugin): pass/fail/
known-defect counts, a proportion bar, the **full failure reason and pytest output
for every failure**, a per-tier breakdown, and every test grouped by module.

It leads with priority: a **P0 banner** naming every failing P0 test, a per-tier
table, and a tier chip on every row. Known defects get their own section rather than
being filed under "skipped" — the suite's 8 `xfail`s each name a real service defect,
and they are the most important thing the run produces.

`make test-report` deliberately lets pytest fail without aborting — a run *with*
failures is exactly the run you want a report for. Every CI job builds the same
report with `if: always()` and ships it in its artifact.

Layers that need a service **skip with an actionable message** when nothing is
listening, rather than failing with a connection error:

```
SKIPPED [11] service at http://localhost:8000 was not ready within 30s
            (last: ConnectError: All connection attempts failed).
            Start it with `make run-service` or
            `docker compose -f service/docker-compose.yml up -d`, or point BASE_URL elsewhere.
```

## Starting the service under test

```bash
make run-service   # local: uvicorn on :8000, SQLite, no simulated latency
make docker-up     # containerised: PostgreSQL + a 40ms simulated per-request delay
make docker-down   # stop and drop the volume (state survives `down` without -v)
```

`make docker-up` polls `/health` until the service actually answers. It does not
sleep: `docker compose up` returns when the container starts, not when uvicorn is
serving, and the compose file gives the `api` service no healthcheck to wait on.

**The two environments are not comparable for performance.** Docker sets
`SIMULATED_LATENCY_MS=40`, which is roughly 30× the entire local request cost, and
it runs PostgreSQL rather than SQLite. Full analysis in
[notes/docker-vs-local.md](notes/docker-vs-local.md); it is why the perf job
reports rather than gates.

## Configuration

Everything is environment-driven with working defaults — no host, port or budget
is hardcoded in a test body.

| Variable | Default | Purpose |
|---|---|---|
| `BASE_URL` | `http://localhost:8000` | Target for the e2e / perf / ui layers |
| `READINESS_TIMEOUT_S` | `30` | How long to wait for `/health` before skipping |
| `HYPOTHESIS_PROFILE` | `ci` | `ci` = 50 examples, derandomised. `nightly` = 1000, random |
| `SCHEMATHESIS_MAX_EXAMPLES` | `15` | Fuzz budget per operation (200 nightly) |
| `PERF_P95_BUDGET_MS` | `250` | p95 budget for the latency smoke |
| `PERF_SAMPLES` / `PERF_WARMUP` | `100` / `20` | Latency sample size |
| `CONCURRENCY_WRITERS` | `25` | Simultaneous writers in the concurrency test |
| `TEST_ENV_LABEL` | `local` | Recorded next to any published perf number |

## Layout

```
tests/
  conftest.py          fixtures: per-test isolated DB, live client, readiness skip
  helpers/             config, builders, assertions, client construction
  unit/                scoring boundaries + Hypothesis properties        (no I/O)
  contract/            OpenAPI conformance + schemathesis fuzzing
  api/                 in-process integration against an isolated database
  e2e/                 live HTTP: deployment surface only
  perf/                latency budget + concurrent-write invariants
  ui/                  one Playwright dashboard smoke
tools/mutation_check.py   proves the suite catches real bugs
tools/test_report.py      JUnit XML -> self-contained HTML report
.github/workflows/        pr.yml (gates) · main.yml (+e2e, +perf) · nightly.yml
```

Markers are registered and `--strict-markers` is on, so a typo in a marker is an
error rather than a silently-skipped test. Layer: `unit`, `contract`, `api`, `e2e`,
`perf`, `ui`, `slow`. Priority: `p0`, `p1`, `p2`.

## Verifying the suite actually catches bugs

```bash
python tools/mutation_check.py
```

It copies `service/` to a scratch directory, breaks it in eight specific ways, and
reports which tests died. `service/` itself is never touched. Seven mutants are real
bugs and must be killed; the eighth is a no-op control that must survive — without
it, "everything went red" would be unfalsifiable. Last run:
[notes/mutation-check.md](notes/mutation-check.md).

## A note on what is red on purpose

The suite reports **8 xfails**. They are not noise: each one asserts the behaviour
the service *should* have, with `strict=True`, so that fixing the service turns
the XPASS into a build failure and forces the known-issues list to be updated.
They are catalogued in [TEST_STRATEGY.md](TEST_STRATEGY.md#known-service-issues).

Expected output of `make test`:

```
86 passed, 18 deselected, 5 xfailed
```
