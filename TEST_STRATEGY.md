# Test strategy — NOC Station Health API

This service decides which EV chargers a technician drives to. Every decision below
follows from one asymmetry in that job: a score that is wrong *low* costs a wasted
90-minute drive; a score that is wrong *high* leaves a dead charger on a highway
corridor until a driver at 20% battery finds it. The second failure is expensive,
silent, and where I put the weight.

This document is the argument; the workings are in [`notes/`](notes/) — behaviour
inventory, full risk register, Docker analysis, mutation-check output, AI journal.

## What recon changed

I stood the service up and curl'd every endpoint before writing a test
([`notes/behaviour-inventory.md`](notes/behaviour-inventory.md)). Two facts
contradicted my starting assumptions and drove everything after:

1. **It is not in-memory.** SQLAlchemy over SQLite locally, PostgreSQL in Docker;
   state survives a restart. Isolation is a *database* problem, not a process one.
2. **`POST /reports` is append-only.** Nothing is updated or deduplicated, and each
   read endpoint independently recomputes "latest per station" with a
   `GROUP BY … MAX(timestamp)` join. Every interesting defect falls out of that.

**Eight service defects found**, three by tooling rather than by reading code. They
are catalogued under [Known service issues](#known-service-issues).

## Layer weighting follows the risk register, not the pyramid

The register ([`notes/risk-register.md`](notes/risk-register.md)) scores each
failure mode by (blast radius × likelihood) ÷ detection cost. Every test docstring
cites a risk ID; a test that cannot cite one should not exist. The top of the
ranking was not what I expected:

| Rank | Risk | Statement |
|---|---|---|
| 1 | **R2** | An offline station reporting 0 latency and 0 errors scores **exactly 60.0**, and the flag test is `score < 60`. A completely dead charger is never flagged. |
| 2 | **R7** | Any change to a scoring constant re-scores the whole fleet at once. |
| 3 | **R16** | The service violates its own published `format: date-time`. |
| 4 | **R1** | A retried report double-counts its station in `/stations` and in every `/metrics/summary` number. |
| 5 | **R3** | One clock-skewed station pins its own status forever; later real reports are invisible. |

109 tests:

| Layer | Tests | Why this much |
|---|---|---|
| Unit boundaries | 26 | R2 and R7 are the top two risks and both are pure-function bugs — the cheapest place to catch the most damage. |
| Property-based | 15 | The scoring function is pure with real invariants. Found R15. |
| API integration | 43 | Heaviest layer: R1, R3, R5, R6, R11 and R18 are all cross-endpoint or ordering defects no unit test can see. |
| Contract | 13 | The service publishes its own schema, so conformance is nearly free. Found R9 and R16. |
| E2E | 5 | Deliberately thin — only what in-process testing cannot cover. |
| Perf + concurrency | 6 | Reports, never gates. |
| UI | 1 | One smoke test. |

**Why not a pyramid.** This service's logic is 40 lines of arithmetic, while its
risk lives in three SQL queries that each reinvent "the latest report per station".
Weighting by the register rather than by shape puts the mass where the bugs are —
which is why integration is the biggest layer, and why I would not defend that
split for a different service.

## Priority tiers: P0 / P1 / P2

Layer answers *where* a test runs. Tier answers the question that matters during a
bad build: **which red test do I read first?** Every test declares exactly one,
derived from the blast radius of the risk it covers. Per-test rationale is in
[`notes/risk-register.md`](notes/risk-register.md#priority-tiers).

| Tier | Means | Tests | If it is red |
|---|---|---|---|
| **P0** | The service is doing its core job wrong: the score, the flag decision, which report counts as latest, or the endpoints disagreeing about one station. | 42 (39%) | Stop. Do not ship, and do not investigate anything else first. |
| **P1** | A real defect, narrower blast radius or a specific edge: validation contract, schema conformance, metric robustness, timezone semantics, saturation and rounding edges, concurrency invariants. | 53 (49%) | Fix before release; it does not block the next person's merge. |
| **P2** | Worth having, not worth blocking on: response ordering, deployment surface, framework-default pinning, perf, the UI smoke. | 14 (13%) | File it. Information, not an emergency. |

**Tier is derived from the register but is not a re-ranking of it**, and the
disagreements are the interesting part. R16 is rank 3 in the register yet its tests
are P1, because the damage is client-side deserialisation rather than a wrong
dispatch — an operator with a mangled timestamp still gets the right station on the
right worklist. Conversely, the test that a client cannot post its own
`hygiene_score` covers no numbered risk and is P0, because that would defeat the
entire point of the service.

**Enforced, not aspirational.** `tests/conftest.py` refuses to collect a test that
declares no tier, or two. A silent default would have been easier and worse: the
tests that most need triage are the ones written in a hurry, so they are exactly
the ones that would have inherited it. Tiers are per-test rather than a module-level
`pytestmark` because pytest cannot remove an inherited marker from one item — with a
module default, `-m p0` would also match the P1 tests in a P0 module. That same hook
stamps the tier into the JUnit XML, so the HTML report reads it instead of
re-deriving it and can never disagree with the run about what is P0.

```bash
make test-p0    # or `make smoke` — 37 tests, ~1s
pytest -m "p0 and api"          # tiers compose with layers
```

**I did not split the CI pipeline by tier.** It is the obvious next move and it
would be waste here: the gate is 109 tests in ~1.5 s, so a P0-first job would spend
~40 s of runner setup to save one and a half, and `needs:` already keeps the
container jobs from starting behind a broken gate. The tiers pay off instead in
triage order, a seconds-long local `make smoke`, and a P0 banner in the report. If
the gate ever ran in minutes, the split would be P0+P1 blocking on PRs with P2
moved post-merge — the markers are already there for that day.

## Tools, and the alternative each one beat

- **httpx** over `requests` — the concurrency test needs `AsyncClient` to fire 25
  simultaneous writers; `requests` would mean a threadpool measuring a threadpool.
  Bonus: Starlette's `TestClient` *is* an `httpx.Client` subclass, so the in-process
  and live clients share one interface.
- **Hypothesis** because `compute_hygiene_score` is a pure function over a small
  domain — the one place here where property-based testing is the right tool rather
  than an impressive-sounding one. It falsified two invariants I wrote by hand
  (R15). I deliberately did **not** point it at the HTTP layer: that needs a
  function-scoped DB fixture shared across examples, the classic anti-pattern.
- **jsonschema** over an OpenAPI-specific validator — OpenAPI 3.1 *is* JSON Schema
  2020-12, so no translation layer. One trade-off: `format` is an annotation, so
  `jsonschema` ignores it by default.
- **schemathesis** because the service publishes its own schema. It validates
  `format`, and the gap between its strictness and `jsonschema`'s found **R16**; it
  also generated `error_count: 2**63` and found **R17**. Two tools with different
  defaults on one schema is not redundancy — it is where the findings were.
- **pytest-randomly** — order-dependence fails loudly instead of hiding. It exposed
  cross-test pollution from schemathesis' ASGI transport leaking anyio streams.
- **Playwright** for the one UI test: role/text selectors and auto-retrying
  assertions, so no `wait_for_timeout`. One cost I hit — a failed browser launch
  leaves its event loop in a state that kills unrelated asyncio and `TestClient`
  tests in the same process, so `make test-all` runs the UI layer separately.
- **ruff + `mypy --strict`** over tests *and* tools. Tests are production code.

## Test data and isolation — the decision I care most about

No reset endpoint, real persistent state, so isolation is entirely on me:

| Option | Verdict |
|---|---|
| Unique station IDs per test | Parallel-safe, no teardown — but `/metrics/summary` aggregates over *every* station, so no test could assert `total_stations == 2`. Rejected in-process; **adopted for e2e**, where nothing else is possible. |
| Truncate between tests | Fast and exact, but serial-only: two workers would delete each other's rows. Rejected. |
| Re-import the app per test | Correct but slow, and depends on module-reload semantics that break as soon as anything caches the app. Rejected. |
| **Override the `get_db` dependency** | **Chosen.** FastAPI already exposes the seam (`database.py:15`), so each test binds the app to its own engine: empty database, exact aggregate assertions, parallel-safe, and zero changes to `service/`. |

Choosing the cheap option would have cost real findings: **R1, R5 and R18 are only
visible in network-wide aggregates.** So the suite runs **dependency overrides
in-process and ID namespacing over the wire**, where every e2e assertion is a delta
(`after == before + 1`) because that database is shared.

**Builders, not fixtures-per-scenario** — one `report()` builder defaulting to the
README's sample payload, so the interesting field is visible at the call site.
Timestamps are fixed offsets from a constant, never `now()`: this service ranks by
client-supplied timestamp, so a wall-clock read would make recency assertions racy.

**In-process vs live HTTP: both, split by what each can prove.** `TestClient` never
serialises a real response, never starts uvicorn, never mounts static files from
disk, never touches PostgreSQL — so it gets the fast, hermetic, exact-assertion
work, and e2e covers only the deployment surface. One control test asserts the two
transports agree on the same input; if it fails, this split is wrong.

## CI: what blocks a merge, and what merely reports

| Trigger | Runs | Blocks? |
|---|---|---|
| **PR** | ruff, `mypy --strict`, unit + contract + api | **Yes** — hermetic, deterministic, ~90 s. Nothing can fail for a reason unrelated to the diff. |
| **Push to main** | the above + e2e against `docker compose` | **Yes** — real HTTP, real PostgreSQL, real startup path. Too slow for every push; too important to leave broken on main. |
| **Push to main** | perf + concurrency smoke | **No** — `continue-on-error`, separate job. |
| **Nightly** | Hypothesis 1000 examples, schemathesis 200/operation, UI | **No** — breadth over speed. |

**Why perf reports rather than blocks.** `docker-compose.yml:27` sets
`SIMULATED_LATENCY_MS=40` and `main.py:23-27` sleeps that long on every request
including `/health`; the measured local baseline is ~1.3 ms. The *environment*
changes the number 30× before any code does, and neither environment pins CPU
(compose declares no limits; runners are shared). So the budget comes from config
(`PERF_P95_BUDGET_MS`, default 250 ms — sized for an order-of-magnitude regression
and nothing finer), the numbers and environment label are printed into the job
summary on every run, and it is a *separate* job so a red perf smoke is visually
distinct from a red unit suite. Full analysis:
[`notes/docker-vs-local.md`](notes/docker-vs-local.md).

**Why the fuzz suite is nightly.** Bounded fuzzing is fast until it finds
something; shrinking R17 costs ~50 s, a third of the gate's budget. It is marked
`slow`, and the gate excludes by *marker*, so a slow test added to a fast layer
leaves the gate automatically.

**Readiness, not sleeps.** `docker compose up` returns when the container starts,
not when uvicorn serves — and compose gives the `api` service no healthcheck to
wait on. Every job polls `/health` with a deadline and fails with an actionable
message. There is no sleep-based *synchronisation* in this suite; the one
`time.sleep` is the 250 ms interval inside that poll, which returns the instant the
condition holds.

Also: concurrency groups with `cancel-in-progress` on PRs (not on main, whose
results are a record), pip cached, actions pinned to major tags, JUnit XML and
coverage published as artifacts.

## Did I actually test anything?

A green suite proves nothing, so I checked. `tools/mutation_check.py` copies
`service/` to a scratch directory, applies one mutation at a time, and runs the
merge-gate layers against the copy — the real service is never touched.
Full output: [`notes/mutation-check.md`](notes/mutation-check.md).

**16 mutants, 15 killed, 1 survivor — and the survivor is the point:** the last is
a deliberate no-op, so if the harness reported it killed, every result above it
would be worthless. Two uncomfortable results: `recency-inverted`
(`ORDER BY timestamp DESC` → `ASC`) is killed by **only three tests, all in one
file** — a narrow net under the worst realistic bug here — and
`metrics-errors-over-all-history` by **exactly one**. Both are real gaps, not
rhetorical modesty; they are items 1 and 2 below. I ran this by hand rather than
adding `mutmut`, because at this size the value is in choosing mutations a person
would actually make.

## Known service issues

Each has a test. The `xfail(strict=True)` ones assert the behaviour I believe is
*correct*, so fixing the service turns the XPASS into a build failure and forces
this list to be updated — the only way a known-bug marker stays honest. Full
write-ups, blast radius and reproductions: [`notes/risk-register.md`](notes/risk-register.md).

| ID | Issue | Pinned in |
|---|---|---|
| **R2** | Offline + 0 latency + 0 errors → exactly 60.0, **not** flagged. Code and README agree, so this is a *specification* defect: pinned as-is rather than inventing a threshold. Highest-severity finding here. | `unit/test_scoring_boundaries.py` |
| **R1** | Any at-least-once retry lists the station twice and inflates every metric. `/stations/{id}/status` is unaffected, which is how it survives a manual check. | 2 × `xfail(strict)`, `api/` |
| **R3** | A future-dated report permanently masks every later one, leaving the station green forever. Pinned, not xfailed — the fix is a product decision (reject? clamp? rank by the already-stored `created_at`?). | `api/test_recency_semantics.py` |
| **R16** | `latest_timestamp` declares `format: date-time` but the service emits naive datetimes — it violates its own schema. Found by schemathesis. | `xfail(strict)`, `contract/` |
| **R17** | `error_count: 2**63` passes validation and overflows the driver → unhandled **500** (`2**63 - 1` is fine). Fix is a `le=` bound. Found by schemathesis. | `xfail(strict)` + boundary test, `api/` |
| **R18** | `Infinity` latency is accepted and `average_latency_ms` then serialises as `null` — indistinguishable from an empty network, and no alert fires on null. | `api/test_cross_endpoint_consistency.py` |
| **R15** | The score is rounded *last*, so R2's blind spot is an interval (every offline station under 0.1 ms), not a point. Found by Hypothesis. | `unit/test_scoring_properties.py` |
| **R9** | A 404 the schema does not declare, whose `detail` is a *string* where the 422's is a *list*. | `xfail(strict)`, `contract/` |
| **R8** | `/health` never touches the database: a liveness probe used as a readiness probe, so a rollout proceeds into a deploy that 500s on every read. Documented rather than asserted — the defect is that nothing else exists. | docstring, `e2e/` |
| **R10** | `/stations/poor-hygiene` has no `ORDER BY`, so the worklist reshuffles between engines. Asserted as a set, never a list. | `api/` |

Two behaviours I judged **not** defects but pinned anyway, because nobody chose
them: extra JSON fields are silently ignored (notably a client *cannot* inject its
own `hygiene_score`), and numeric strings are coerced. Both are Pydantic defaults
real clients depend on; the risk is a major-version upgrade changing them quietly.

## What I deliberately did not do

1. **Data-volume / query-degradation testing (R13).** ~260M rows a year at 500
   stations reporting each minute, and every read scans the whole table. Automating
   it means seeding tens of millions of rows, which against SQLite on a shared
   runner measures the runner's disk. Belongs in a staging soak. Instead: the
   arithmetic is on the record, plus an affordable slice in the perf layer
   (already super-linear at 200 rows? No).
2. **Payload-size / resource-exhaustion testing (R12).** With no body limit
   anywhere, a 100 MB test could only pin "unbounded" as correct or fail forever.
   Limits belong at the ingress — and if hostile callers are in the threat model,
   the missing control is *authentication*, which this service has none of.
   Instead: a 10 KB firmware string round-trips intact, and the gap is documented.
3. **Deep UI automation.** One static HTML file, no build, no framework, no router.
   Page objects and a browser matrix would cost more than the rest of the suite, to
   catch a JS typo. One smoke test, nightly.
4. **A real load harness (k6/Locust).** Against one uvicorn worker behind a 40 ms
   artificial delay, it measures the delay and the runner.
5. **Mocking the scoring function in API tests.** R7 *is* that the real constants
   change; a mocked test passes against every mutation of the thing it protects. The
   API layer computes expected values from a second implementation of the published
   formula instead — deliberate duplication, and the mutation check proves it works.
6. **Asserting FastAPI's validation prose.** `status`, `loc` and `type` are the
   contract; `msg` belongs to Pydantic and changes on minor upgrades.

## Gaps and what I would do next

1. **Widen the net under recency** — cross-endpoint recency assertions on
   `/stations` and `/metrics/summary`, not just the detail view.
2. **Second cover for the latest-per-station aggregation** — one test between the
   service and a silently wrong network error total is one too few.
3. **Run the API layer against PostgreSQL, not only via e2e.** The `GROUP BY`
   semantics behind R1 differ between engines and the fast layers only ever see
   SQLite; a `testcontainers` fixture behind a marker would likely surface more of R11.
4. **A stateful Hypothesis model of the ingest/read cycle.** The invariants are
   already written prose in the register: distinct IDs equals `total_stations`, the
   worklist equals the flagged subset, status always reflects `MAX(timestamp)`. A
   `RuleBasedStateMachine` would search *sequences* rather than the cases I thought
   of. Highest-value addition; I ran out of time.
5. **Consumer-driven contract tests.** Today's contract layer validates against the
   service's own schema, so it cannot catch "schema and service are both wrong".
6. **Close R8.** A readiness probe that touches the database is a service change, so
   I only documented it — but it is the first ticket I would file: it is the one
   defect that makes a bad deploy invisible to the platform.

## Caveats on my own numbers

**Docker was not installed on the machine this suite was developed on.** The local
latency figures are measured; the Docker-side effects are derived from configuration
and stated as such. The e2e, perf and UI layers were exercised against a live local
uvicorn instance — real HTTP, real serialisation, SQLite — and are written to run
against `docker compose` in CI. So the one thing I have not observed first-hand is
the latest-per-station join running on PostgreSQL, which is exactly the difference
the e2e job exists to cover; I would watch its first run before trusting it.
