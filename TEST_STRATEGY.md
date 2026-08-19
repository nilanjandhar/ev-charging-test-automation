# Test strategy — NOC Station Health API

This service decides which EV chargers a technician drives to. Every decision below
follows from one asymmetry: a score that is wrong *low* costs a wasted 90-minute
drive; a score that is wrong *high* leaves a dead charger on a highway corridor
until a driver at 20% battery finds it. The second failure is silent, and it is
where I put the weight.

This document is the argument. The workings are in [`notes/`](notes/) — behaviour
inventory, risk register, Docker analysis, mutation-check output, AI journal.

I stood the service up and curl'd every endpoint before writing a test. Two facts
came out of that hour and drove everything after:

1. **It is not in-memory.** SQLAlchemy over SQLite locally, PostgreSQL in Docker;
   state survives a restart. Isolation is a *database* problem, not a process one.
2. **`POST /reports` is append-only.** Nothing is updated or deduplicated, and each
   read endpoint independently recomputes "latest per station" with a
   `GROUP BY … MAX(timestamp)` join. Every interesting defect falls out of that.

**Eight service defects found**, three of them by tooling rather than by reading
code. They are listed under [Known service issues](#known-service-issues).

## What I test, and how much

The register ([`notes/risk-register.md`](notes/risk-register.md)) scores each
failure mode by (blast × likelihood) ÷ detection cost. It is the reasoning behind
what to test; the tests themselves name their failure mode in plain words rather
than cross-referencing an ID, so a docstring is readable without a second file
open. The top of the ranking was not what I expected:

| Rank | Risk | Statement |
|---|---|---|
| 1 | **R2** | Offline + 0 latency + 0 errors scores **exactly 60.0**, and the flag test is `score < 60`. A dead charger is never flagged. |
| 2 | **R7** | Any change to a scoring constant re-scores the whole fleet at once. |
| 3 | **R16** | The service violates its own published `format: date-time`. |
| 4 | **R1** | A retried report double-counts its station in `/stations` and every metric. |
| 5 | **R3** | One clock-skewed station pins its own status forever. |

**44 test functions, 74 collected** (parametrised cases expand): 16 unit boundaries,
8 property-based, 31 API integration, 11 contract, 4 e2e, 3 perf/concurrency, 1 UI.

Every test carries a `Why:` line in its docstring naming what goes unnoticed
without it, and `make inventory` fails if one is missing — so a test that cannot be
justified cannot be added. [`notes/test-inventory.md`](notes/test-inventory.md) is
that list. I cut 18 tests and 17 parametrised cases getting here: eight were
assertions another test already made universally (a unit example of a saturation
the property covers for all inputs), and ten pinned undocumented behaviour or
framework defaults. **All 8 mutants still die**, which is the evidence the cuts
took redundancy rather than coverage.

**Why that shape rather than a pyramid.** This service's logic is 40 lines of
arithmetic; its risk lives in three SQL queries that each reinvent "the latest
report per station". So the integration layer is the heaviest — R1, R3, R5, R6, R11
and R18 are all cross-endpoint or ordering defects no unit test can see — while the
unit layer stays cheap and dense because R2 and R7 are pure-function bugs. E2E is
deliberately thin: only what in-process testing cannot cover. I would not defend
this split for a different service.

## Priority tiers: P0 / P1 / P2

Layer says *where* a test runs. Tier answers the question that matters during a bad
build: **which red test do I read first?** Per-test rationale is in
[`notes/risk-register.md`](notes/risk-register.md#priority-tiers).

| Tier | Means | Tests | If it is red |
|---|---|---|---|
| **P0** | The service is doing its core job wrong: the score, the flag decision, which report counts as latest, or the endpoints disagreeing about one station. | 33 | Stop. Do not ship. |
| **P1** | A real defect, narrower blast radius or a specific edge. | 37 | Fix before release. |
| **P2** | Worth having, not worth blocking on. | 4 | File it. |

Tier is derived from the register but is not a re-ranking of it, and the
disagreements are the interesting part. R16 is rank 3 yet its tests are P1, because
the damage is client-side deserialisation rather than a wrong dispatch — an operator
with a mangled timestamp still gets the right station on the right worklist.
Conversely, the test that a client cannot post its own `hygiene_score` covers no
numbered risk and is P0, because that would defeat the point of the service.

P2 is down to four because most of what used to sit there was pinning behaviour
nobody promised — which is a reason to delete a test, not to downgrade it.

`tests/conftest.py` refuses to collect a test that declares no tier, or two. A
silent default would have been easier and worse: the tests that most need triage are
the ones written in a hurry. The same hook stamps the tier into the JUnit XML, so
the HTML report reads it rather than re-deriving it.

```bash
make smoke                # P0 only, ~1s
pytest -m "p0 and api"    # tiers compose with layers
```

**I did not split CI by tier.** The gate is 74 tests in ~1.3 s, so a P0-first job
would spend ~40 s of runner setup to save one and a half, and `needs:` already stops
the container jobs starting behind a broken gate. If the gate ever ran in minutes,
the split would be P0+P1 blocking with P2 post-merge — the markers are already there.

## Tools, and the alternative each beat

- **httpx** over `requests` — the concurrency test needs `AsyncClient`; `requests`
  would mean a threadpool measuring a threadpool. Starlette's `TestClient` *is* an
  `httpx.Client`, so in-process and live clients share one interface.
- **Hypothesis** because `compute_hygiene_score` is a pure function over a small
  domain — the one place here where it is the right tool rather than an
  impressive-sounding one. It falsified two invariants I wrote by hand (R15). I
  deliberately did not point it at the HTTP layer: that needs a function-scoped DB
  fixture shared across examples, the classic anti-pattern.
- **jsonschema** over an OpenAPI-specific validator — OpenAPI 3.1 *is* JSON Schema
  2020-12, so no translation layer. One trade-off: it ignores `format` by default.
- **schemathesis** because the service publishes its own schema. It validates
  `format`, and the gap between its strictness and `jsonschema`'s found **R16**; it
  also generated `error_count: 2**63` and found **R17**. Two tools with different
  defaults on one schema is not redundancy — it is where the findings were.
- **pytest-randomly** — order-dependence fails loudly. It exposed cross-test
  pollution from schemathesis' ASGI transport leaking anyio streams.
- **Playwright** for the one UI test: role/text selectors and auto-retrying
  assertions, so no `wait_for_timeout`.
- **ruff + `mypy --strict`** over tests *and* tools. Tests are production code.

## Test data and isolation

The service keeps state in a real database with no reset endpoint, so isolation is
entirely on me:

| Option | Verdict |
|---|---|
| Unique station IDs per test | Parallel-safe, no teardown — but `/metrics/summary` aggregates over *every* station, so no test could assert `total_stations == 2`. Rejected in-process; **adopted for e2e**, where nothing else is possible. |
| Truncate between tests | Fast and exact, but serial-only: two workers would delete each other's rows. |
| Re-import the app per test | Correct but slow, and depends on module-reload semantics that break as soon as anything caches the app. |
| **Override the `get_db` dependency** | **Chosen.** FastAPI already exposes the seam, so each test binds the app to its own engine: empty database, exact aggregate assertions, parallel-safe, zero changes to `service/`. |

The cheap option would have cost real findings: **R1, R5 and R18 are only visible in
network-wide aggregates.** So the suite runs dependency overrides in-process and ID
namespacing over the wire, where every e2e assertion is a delta
(`after == before + 1`) because that database is shared.

**Builders, not fixtures-per-scenario** — one `report()` builder defaulting to the
README's sample payload, so the interesting field is visible at the call site.
Timestamps are fixed offsets from a constant, never `now()`: this service ranks by
client-supplied timestamp, so a wall-clock read would make recency assertions racy.

`TestClient` never serialises a real response, starts uvicorn, mounts static files or
touches PostgreSQL — so it gets the fast, exact-assertion work, and e2e covers only
the deployment surface. One control test asserts the two transports agree; if it
fails, this split is wrong.

## CI: what blocks a merge, and what reports

| Trigger | Runs | Blocks? |
|---|---|---|
| **PR** | ruff, `mypy --strict`, unit + contract + api | **Yes** — hermetic, deterministic, ~90 s |
| **Push to main** | the above + e2e against `docker compose` | **Yes** — real HTTP, real PostgreSQL, real startup path |
| **Push to main** | perf + concurrency smoke | **No** — `continue-on-error`, separate job |
| **Nightly** | Hypothesis 1000 examples, schemathesis 200/operation, UI | **No** |

**Why perf reports rather than blocks.** Docker sets `SIMULATED_LATENCY_MS=40` and
sleeps that long on every request including `/health`; the local baseline is ~1.3 ms.
The environment moves the number 30× before any code does, and neither environment
pins CPU. So the budget comes from config (`PERF_P95_BUDGET_MS`, default 250 ms —
sized for an order-of-magnitude regression, nothing finer), the numbers and
environment are printed on every run, and it is a *separate* job so a red perf smoke
is visually distinct from a red unit suite. Detail:
[`notes/docker-vs-local.md`](notes/docker-vs-local.md).

**Why the fuzz suite is nightly.** Shrinking the R17 overflow costs ~50 s, a third of
the gate's budget. It is marked `slow`, and the gate excludes by *marker*, so a slow
test added to a fast layer leaves the gate automatically.

**Readiness, not sleeps.** `docker compose up` returns when the container starts, not
when uvicorn serves, and compose gives the `api` service no healthcheck. Every job
polls `/health` with a deadline. There is no sleep-based *synchronisation* in the
suite; the one `time.sleep` is the interval inside that poll.

## Did I actually test anything?

`tools/mutation_check.py` copies `service/` to a scratch directory, applies one
mutation at a time, and runs the gate against the copy — the real service is never
touched. Output: [`notes/mutation-check.md`](notes/mutation-check.md).

**8 mutants, 7 killed, 1 survivor — and the survivor is the point:** the last is a
deliberate no-op, so if the harness reported it killed, every result above it would
be worthless. One uncomfortable result: `recency-inverted` (`ORDER BY timestamp DESC`
→ `ASC`) is killed by only three tests, all in one file — a narrow net under the
worst realistic bug here. That is gap #1 below, not rhetorical modesty.

## Known service issues

Each has a test. The `xfail(strict=True)` ones assert the behaviour I believe is
correct, so fixing the service turns the XPASS into a build failure and forces this
list to be updated. Full write-ups:
[`notes/risk-register.md`](notes/risk-register.md).

| ID | Issue | Pinned in |
|---|---|---|
| **R2** | Offline + 0 latency + 0 errors → exactly 60.0, **not** flagged. Code and README agree, so this is a *specification* defect: pinned as-is rather than inventing a threshold. Highest-severity finding. | `unit/` |
| **R1** | Any at-least-once retry lists the station twice and inflates every metric. `/stations/{id}/status` is unaffected, which is how it survives a manual check. | 2 × `xfail(strict)`, `api/` |
| **R3** | A future-dated report permanently masks every later one. Pinned, not xfailed — the fix is a product decision. | `api/` |
| **R16** | `latest_timestamp` declares `format: date-time` but the service emits naive datetimes. Found by schemathesis. | `xfail(strict)`, `contract/` |
| **R17** | `error_count: 2**63` passes validation and overflows the driver → unhandled **500**. Found by schemathesis. | `xfail(strict)` + boundary test, `api/` |
| **R18** | `Infinity` latency is accepted and `average_latency_ms` serialises as `null` — indistinguishable from an empty network, and no alert fires on null. | `api/` |
| **R15** | The score is rounded *last*, so R2's blind spot is an interval, not a point. Found by Hypothesis. | `unit/` |
| **R9** | A 404 the schema does not declare, whose `detail` is a *string* where the 422's is a *list*. | `xfail(strict)`, `contract/` |
| **R8** | `/health` never touches the database: a liveness probe used as readiness, so a rollout proceeds into a deploy that 500s on every read. | docstring, `e2e/` |
| **R10** | `/stations/poor-hygiene` has no `ORDER BY`. Asserted as a set, never a list. | `api/` |

Two behaviours I judged *not* defects but pinned anyway, because nobody chose them:
extra JSON fields are silently ignored (notably a client cannot inject its own
`hygiene_score`), and numeric strings are coerced. Both are Pydantic defaults real
clients depend on; the risk is a major-version upgrade changing them quietly.

## What I deliberately did not do

1. **Data-volume testing (R13).** ~260M rows a year at 500 stations reporting each
   minute, and every read scans the whole table. Automating it means seeding tens of
   millions of rows, which against SQLite on a shared runner measures the runner's
   disk. Belongs in a staging soak. Instead: the arithmetic is on the record, plus an
   affordable slice in the perf layer (already super-linear at 200 rows? No).
2. **Payload-size testing (R12).** With no body limit anywhere, a 100 MB test could
   only pin "unbounded" as correct or fail forever. Limits belong at the ingress —
   and if hostile callers are in the threat model, the missing control is
   *authentication*, which this service has none of.
3. **Deep UI automation.** One static HTML file, no build, no framework. Page objects
   and a browser matrix would cost more than the rest of the suite, to catch a JS
   typo. One smoke test, nightly.
4. **A real load harness.** Against one uvicorn worker behind a 40 ms artificial
   delay, it measures the delay and the runner.
5. **Mocking the scoring function in API tests.** R7 *is* that the real constants
   change; a mocked test passes against every mutation of the thing it protects. The
   API layer computes expected values from a second implementation of the published
   formula instead — deliberate duplication, and the mutation check proves it works.
6. **Asserting FastAPI's validation prose.** `status`, `loc` and `type` are the
   contract; `msg` belongs to Pydantic and changes on minor upgrades.

## Gaps and what I would do next

1. **Widen the net under recency** — cross-endpoint recency assertions on `/stations`
   and `/metrics/summary`, not just the detail view.
2. **Run the API layer against PostgreSQL, not only via e2e.** The `GROUP BY`
   semantics behind R1 differ between engines and the fast layers only see SQLite; a
   `testcontainers` fixture behind a marker would likely surface more of R11.
3. **A stateful Hypothesis model of the ingest/read cycle.** The invariants are
   already prose in the register — distinct IDs equals `total_stations`, worklist
   equals the flagged subset, status always reflects `MAX(timestamp)`. A
   `RuleBasedStateMachine` would search *sequences* rather than the cases I thought
   of. Highest-value addition; I ran out of time.
4. **Consumer-driven contract tests.** Today's contract layer validates against the
   service's own schema, so it cannot catch "schema and service are both wrong".
5. **Close R8.** A readiness probe that touches the database is a service change, so I
   only documented it — but it is the first ticket I would file: it is the one defect
   that makes a bad deploy invisible to the platform.

## Caveats on my own numbers

**Docker was not installed on the machine this was developed on**, so while I was
writing this the Docker-side effects were derived from configuration and the
latest-per-station join had never run against PostgreSQL. I said I would watch the
first CI run before trusting it. It has now run:

- **e2e against `docker compose` passed** — the join behaves the same on PostgreSQL
  as on SQLite, which was the open question.
- **The 40 ms prediction was right.** Measured Docker p95 is 42–45 ms across every
  endpoint against a 0.6–1.5 ms local baseline — `/health`, which does no work at
  all, costs 42 ms. Numbers in [`notes/docker-vs-local.md`](notes/docker-vs-local.md).
- **The perf job was red for a reason that had nothing to do with performance**: a
  `tee` writing into `reports/` before pytest created it. Every perf test passed. It
  is fixed, and it is a good argument for the design — had perf been a merge gate,
  that shell bug would have blocked every merge.
