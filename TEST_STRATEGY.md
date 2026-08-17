# Test strategy — NOC Station Health API

## The short version

This service decides which EV chargers a technician drives to. Every testing
decision below follows from one asymmetry in that job: a score that is wrong
*low* costs a wasted 90-minute drive, and a score that is wrong *high* leaves a
dead charger on a highway corridor until a driver at 20% battery finds it. The
second failure is the expensive one, it is silent, and it is where I put the
weight.

I spent the first hour standing the service up and hitting every endpoint with
`curl` before writing a line of test code. That hour produced the findings in
`notes/behaviour-inventory.md` and is the reason this suite tests *this* service
rather than a generic FastAPI app. Two things I would have got wrong otherwise:

1. **This service is not in-memory.** It is SQLAlchemy over SQLite locally and
   PostgreSQL in Docker, and its state survives a process restart. Test isolation
   is therefore a database problem, not a process problem.
2. **`POST /reports` is append-only.** Nothing is updated or deduplicated; every
   read endpoint recomputes "latest per station" with a `GROUP BY … MAX(timestamp)`
   join. Every interesting defect in this service falls out of that one choice.

**Eight defects found, three of them by tooling I would not have written by
instinct.** They are listed under "Known service issues" with reproductions.

---

## What I decided to test, and why

The full risk register is `notes/risk-register.md` — failure mode, route to
production, blast radius for a network operator, likelihood, the layer that
should catch it, and the cost of catching it there, ranked by
(blast × likelihood) ÷ detection cost. It is the backbone of this document. Every
test in the suite cites a risk ID in its docstring; a test that cannot cite one
should not exist.

The top of that register is not what I expected before reading the code:

| Rank | Risk | One-line statement |
|---|---|---|
| 1 | **R2** | An offline station reporting 0 latency and 0 errors scores **exactly 60.0**, and the flag test is `score < 60`. A completely dead charger is never flagged. |
| 2 | **R7** | Any change to a scoring constant re-scores the entire fleet at once. |
| 3 | **R16** | The service violates its own published `format: date-time`. |
| 4 | **R1** | A retried report double-counts its station in `/stations` and every number in `/metrics/summary`. |
| 5 | **R3** | One clock-skewed station pins its own status forever; later real reports are invisible. |

### Layer weighting follows the register, not the pyramid

109 tests total, distributed like this:

| Layer | Tests | Share | Why this much |
|---|---|---|---|
| Unit (boundaries) | 26 | 24% | R2 and R7 are the top two risks and both are pure-function bugs. This is the cheapest place to catch the most damage — a parametrized row costs a line and runs in a millisecond. |
| Property-based | 15 | 14% | The scoring function is pure with real algebraic invariants, so Hypothesis is genuinely applicable rather than fashionable. It found R15, which examples did not. |
| API integration | 43 | 39% | The heaviest layer, because the *interactions* are where this service is weakest: R1, R3, R5, R6, R11 and R18 are all cross-endpoint or ordering defects that no unit test can see. |
| Contract | 13 | 12% | The service publishes its own schema, so conformance is nearly free — and it found R9 and R16. |
| E2E | 5 | 5% | Deliberately thin. Only what in-process testing physically cannot cover. |
| Perf + concurrency | 6 | 6% | Reports, never gates. (Also marked `e2e`: they need a live service.) |
| UI | 1 | 1% | One smoke test. The reasoning is below. |

**Why not a classic pyramid.** A pyramid tells you to write mostly unit tests
because they are cheap. But this service's logic is 40 lines of arithmetic, while
its risk lives in three SQL queries that each independently reinvent "the latest
report per station". Weighting by the register instead of the shape puts the mass
where the bugs are — which is why the integration layer is the biggest one here,
and why I would not defend that split for a different service.

---

## Tool choices, with the alternative I rejected

**pytest** over `unittest` — parametrization with readable IDs is the difference
between one boundary test and eighteen, and `xfail(strict=True)` is load-bearing
in how this suite records known bugs.

**httpx** over `requests` — the concurrency test needs `AsyncClient` to fire 25
simultaneous writers. `requests` would have meant threads, and a threadpool
measuring a threadpool is not a test I would trust. Bonus: Starlette's
`TestClient` is itself an `httpx.Client` subclass, so the in-process and live
clients share one interface and a handful of tests run through both unchanged.

**Hypothesis** because `compute_hygiene_score` is a pure function over a small,
well-defined domain — the one place in this service where property-based testing
is the right tool rather than an impressive-sounding one. It earned its place:
it falsified two invariants I had written by hand, and the reason was R15 (the
score is rounded *last*, so penalties are not exactly additive and a penalty under
0.005 disappears). I would not have found that by inspection. I deliberately did
**not** point Hypothesis at the HTTP layer: that needs a function-scoped database
fixture shared across examples, which is the classic Hypothesis anti-pattern, and
the stateful properties are stated more precisely as explicit tests.

**jsonschema** over an OpenAPI-specific validator — OpenAPI 3.1 *is* JSON Schema
2020-12, so `jsonschema` validates the service's schemas with no translation
layer and no extra dependency. The trade-off is real and worth naming: `format`
is an annotation in JSON Schema, so `jsonschema` ignores it by default. That is
precisely why the next tool matters.

**schemathesis** because the service publishes its own schema, so schema-driven
fuzzing is nearly free coverage of inputs I would not have thought of. It
validates `format` by default, and the gap between its default strictness and
`jsonschema`'s found **R16**. It also generated `error_count: 2**63` and found
**R17**, an unhandled 500 on an unauthenticated endpoint. Two tools with
different defaults, pointed at the same schema, is not redundancy — it is where
the findings were.

**pytest-randomly** — the suite runs in a different order every time, so
order-dependence fails loudly instead of hiding until someone runs a subset. It
has already earned its keep: it exposed cross-test pollution from schemathesis'
ASGI transport leaking anyio streams, where a `ResourceWarning` collected during
GC failed whichever *unrelated* test happened to be running.

**Playwright** for the one UI test — role and text selectors, and its assertions
retry on their own deadline, so the dashboard's async `loadData()` needs no
`wait_for_timeout` and no polling loop of my own. One cost worth recording, since
I hit it: Playwright's sync API drives a greenlet-backed event loop, and when the
browser *fails to launch* it leaves that loop in a state where unrelated asyncio
and `TestClient` tests in the same process die with "Runner.run() cannot be called
from a running event loop". The realistic trigger is installing
`requirements-ui.txt` without running `playwright install`. So `make test-all`
runs the UI layer in its own pytest process — which is also how CI runs it — and
the reason is written into the Makefile rather than left as folklore.

**ruff + mypy --strict** over the test code. Tests are production code. Fixtures
and helpers are type-hinted, and `mypy --strict` passes.

---

## Test data and isolation — the interesting decision

The service holds state in a real database that survives restarts. There is no
reset endpoint. So isolation is entirely on me, and the options were:

| Option | Verdict |
|---|---|
| **Unique station IDs per test** (namespacing, no reset) | Parallel-safe and needs no teardown, but `/metrics/summary` aggregates over *every* station in the database, so no test could ever assert `total_stations == 2`. Aggregates are where R1, R5 and R18 live. Rejected for the in-process layers — and adopted for e2e, where there is no alternative. |
| **Truncate tables between tests** | Fast and exact, but serial-only: two workers would delete each other's rows mid-test. Rejected. |
| **Re-import the app per test against a fresh database file** | Correct but slow, and it depends on module-reload semantics that break the moment anything caches a reference to the app object. Rejected. |
| **Override the `get_db` dependency** ← chosen | FastAPI already exposes the seam (`service/app/database.py:15`). Each in-process test binds the app to *its own* SQLite engine for the duration of that test: empty database, exact aggregate assertions, no truncation, parallel-safe, and not one line of `service/` is touched. |

So the suite uses **two isolation strategies on purpose**: dependency overrides
in-process, and ID namespacing with delta-based assertions over the wire. Every
e2e test asserts `after == before + 1` rather than `total == 1`, because it is
sharing a database with whatever else is deployed there. That constraint is
exactly why the in-process layer exists and is where the aggregate assertions
live.

**Data builders, not fixtures-per-scenario.** One `report()` builder with sensible
defaults (the sample payload from the service README, so the documented happy path
needs no arguments) and per-field overrides, so the interesting field is visible
at the call site instead of buried in `conftest.py`. Timestamps are fixed offsets
from a constant — never `now()` — because this service ranks reports by
client-supplied timestamp, so a wall-clock read would make recency assertions
racy. Station IDs are UUID-based and never asserted on except by identity.

**In-process vs live HTTP.** Both, split by what each can actually prove.
`TestClient` speaks ASGI directly: it never serialises a real response, never
starts uvicorn, never mounts static files from disk, never touches PostgreSQL. So
it gets the fast, hermetic, exact-assertion work, and the e2e layer covers only
the deployment surface — real serialisation, the static mount, the docs routes,
and the latest-per-station join running against PostgreSQL rather than SQLite.
There is one control test asserting the two transports agree on the same input;
if it ever fails, this split is wrong and the doc needs rewriting.

---

## CI: what blocks a merge and what merely reports

| Trigger | Runs | Blocks? | Why |
|---|---|---|---|
| **PR** | ruff, `mypy --strict`, unit, contract, api | **Yes** | Hermetic, deterministic, ~90 s. No container, no network, no wall clock. Nothing here can fail for a reason unrelated to the diff. |
| **Push to main** | the above, plus e2e against `docker compose` | **Yes** | Real HTTP, real PostgreSQL, real startup path. Too slow and too environment-dependent for every PR push, but a broken deployment must not sit on main. |
| **Push to main** | perf + concurrency smoke | **No** (`continue-on-error`, separate job) | See below. |
| **Nightly 03:00 UTC** | Hypothesis broad profile (1000 examples), schemathesis at 200 examples/operation, UI | **No** | Breadth over speed. Opens conversations in the morning; blocks nobody. |

**Why perf reports rather than blocks.** Two reasons, and the first is decisive.
`docker-compose.yml:27` sets `SIMULATED_LATENCY_MS=40`, and `main.py:23-27` sleeps
that long in middleware on *every* request including `/health`. The measured local
baseline for the same endpoints is ~1.3 ms — so the *environment* changes the
number by 30× before any code does. Second, neither environment pins CPU or
memory: compose declares no limits and a GitHub-hosted runner is shared hardware.
A p95 measured there is a weather report, not a contract. So the budget comes from
config (`PERF_P95_BUDGET_MS`, default 250 ms — deliberately generous, sized to
catch an order-of-magnitude regression and nothing finer), the measured numbers
and the environment label are printed into the job summary on every run so the
trend is visible while green, and the job cannot fail the build. It is also a
*separate* job so that a red perf smoke is visually distinct from a red unit
suite — nobody should have to open a log to find out whether main is broken or
merely slow.

**Why the fuzz suite is nightly.** Bounded fuzzing is fast until it finds
something; shrinking the R17 overflow takes ~50 s, which is a third of the gate's
entire budget. It is marked `slow` and the gate excludes by *marker*, so a slow
test added to a fast layer leaves the gate automatically.

**Readiness, not sleeps.** `docker compose up` returns when the container starts,
not when uvicorn is serving — and compose gives the `api` service no healthcheck
to wait on (only `db` has one). Every job that needs the service polls `/health`
with a deadline and fails with an actionable message. There is no sleep-based
*synchronisation* anywhere in this suite — no `sleep(5); assume_ready()`. The one
`time.sleep` in the repository is the 250 ms interval between attempts inside that
poll (`tests/helpers/clients.py:56`), which is the opposite pattern: it returns
the instant the condition holds and fails at a bound rather than under-waiting on
a slow runner.

Also: concurrency groups with `cancel-in-progress` on PRs (and deliberately *not*
on main, whose results are a record), pip cached on the requirements files, action
versions pinned to major tags, JUnit XML and coverage published as artifacts.

---

## Did I actually test anything?

A green suite proves nothing by itself, so I checked. `tools/mutation_check.py`
copies `service/` to a scratch directory, applies one mutation at a time, and runs
the merge-gate layers against the mutated copy — the real service is never
touched. Full output: `notes/mutation-check.md`.

**16 mutants, 15 killed, 1 survivor — and the survivor is the point.** The last
mutant is a deliberate no-op; if the harness reported it as killed, the harness
would be broken and every result above it worthless. Mutants cover the scoring
constants, the flag boundary, the recency ordering, the poor-hygiene filter, the
metrics aggregation, and two call-site slips (swapped arguments, an inverted
stored flag) that leave the scoring function itself untouched.

Three results worth calling out, including the uncomfortable ones:

- `flag-boundary-inclusive` (`score < 60` → `score <= 60`) is killed by 9 tests
  including the R2 boundary test. That single keystroke is the difference between
  flagging every dead station and flagging none, and it goes red loudly.
- `recency-inverted` (`ORDER BY timestamp DESC` → `ASC`) is killed by **only the
  three recency tests**. That is a narrow net under the worst realistic bug in
  the service.
- `metrics-errors-over-all-history` is killed by **exactly one test**. Delete
  `test_metrics_aggregate_only_the_latest_report_per_station` and superseded
  reports could start leaking into the network error total with nothing to catch
  it.

The last two are real gaps, not rhetorical modesty, and they are the first two
items on the "what I would do next" list below.

I ran the mutation check by hand rather than adding `mutmut` to the pipeline: at
this suite's size the value is in choosing *which* mutations describe mistakes a
person would actually make, and a generic mutation tool would spend most of its
runtime on mutants nobody would ever write.

---

## Known service issues

Everything below was found against the unmodified service and reproduced. Each
has a test; the ones marked `xfail(strict=True)` assert the behaviour I believe is
*correct*, so when someone fixes the service the XPASS fails the build and forces
this section to be updated. That is the only way a known-bug marker stays honest.

| ID | Issue | Where it is pinned |
|---|---|---|
| **R2** | An offline station with 0 latency and 0 errors scores exactly 60.0 and is **not** flagged. Code and README agree, so this is a specification defect, not an implementation bug — I pinned current behaviour rather than xfailing it, because I am not entitled to invent a threshold. It is the highest-severity finding here. | `tests/unit/test_scoring_boundaries.py::test_dead_station_reporting_clean_metrics_is_not_flagged` |
| **R1** | A duplicate `(station_id, timestamp)` — i.e. any at-least-once retry — makes the station appear twice in `/stations` and inflates every number in `/metrics/summary`. `/stations/{id}/status` is unaffected, which is how it survives a manual check. | two `xfail(strict=True)` tests in `tests/api/test_cross_endpoint_consistency.py` |
| **R3** | A future-dated report permanently masks every later report. Frozen at "online, 100", the station is permanently green and unmonitorable. Pinned, not xfailed: the fix is a product decision (reject? clamp? rank by `created_at`, which is already stored but never exposed?). | `tests/api/test_recency_semantics.py::test_a_future_dated_report_permanently_masks_every_later_report` |
| **R16** | `latest_timestamp` declares `format: date-time` but the service emits naive datetimes with no offset — it violates its own published schema. Found by schemathesis. | `xfail(strict=True)` in `tests/contract/test_openapi_conformance.py` |
| **R17** | `error_count: 2**63` passes validation and overflows the driver → unhandled **500** on an unauthenticated endpoint. Boundary confirmed: `2**63 - 1` is fine. Fix is a `le=` bound in `schemas.py` so the 422 happens at the edge. Found by schemathesis. | `xfail(strict=True)` in `tests/api/test_ingest_validation.py`, plus a test pinning the largest value that must keep working |
| **R18** | `latency_ms: 1e999` → `Infinity` is accepted, and `average_latency_ms` then serialises as `null` — indistinguishable from a healthy empty network, and no threshold alert can fire on a null. | `tests/api/test_cross_endpoint_consistency.py::test_an_infinite_latency_report_erases_the_network_average_entirely` |
| **R15** | The score is rounded *last*, so the offline penalty is 40 ± 0.01 and any penalty under 0.005 vanishes — meaning R2's blind spot is an interval (every offline station under 0.1 ms), not a single point. Found by Hypothesis. | `tests/unit/test_scoring_properties.py` |
| **R9** | `/stations/{id}/status` returns a 404 that its OpenAPI document does not declare, so a generated client has no branch for it. Its `detail` is a *string* where the 422's is a *list*. | `xfail(strict=True)` in `tests/contract/test_openapi_conformance.py` |
| **R8** | `/health` returns `{"status":"ok"}` without touching the database. It is a liveness probe being used as a readiness probe: a rollout proceeds into a deploy that 500s on every read. Stated in the e2e test's docstring rather than asserted, because the current behaviour is correct *for a liveness probe* — the defect is that nothing else exists. | `tests/e2e/test_live_service.py::test_health_endpoint_answers_over_real_http` |
| **R10** | `/stations/poor-hygiene` has no `ORDER BY`, so the worklist reshuffles between refreshes and differs between SQLite and PostgreSQL. Tests assert set equality, never order — pinning today's accident would be a test that passes locally and fails in Docker. | asserted as a set in `test_flagged_stations_agree_across_list_worklist_and_metrics` |

Two behaviours I found and decided are **not** defects, but pinned anyway because
nobody chose them: extra JSON fields are silently ignored (`extra="ignore"`, and
notably a client *cannot* inject its own `hygiene_score` — worth a regression
test), and numeric strings are coerced (`"120"` → `120.0`). Both are Pydantic
defaults that real embedded clients depend on; the risk is that they change under
a major-version upgrade without anyone noticing.

---

## What I deliberately did not do

**1. Data-volume and query-degradation testing (R13).** Every read endpoint scans
the whole table with a `GROUP BY` and there is no retention, archival, or index on
`timestamp`. At 500 stations reporting once a minute that is ~260M rows a year.
It is a real risk and I would file it — but automating it means seeding tens of
millions of rows, and against SQLite on a GitHub runner that measures the runner's
disk, not the service. It belongs in a staging soak with query-time dashboards.
What I did instead: the growth arithmetic is on the record, and the perf layer
includes an affordable slice — is `/metrics/summary` already super-linear at 200
rows? (It is not.)

**2. Payload-size and resource-exhaustion testing (R12).** There is no body-size
limit anywhere: no middleware, no `max_length` on the string fields, no uvicorn
cap. A test that POSTs 100 MB would assert a limit this service does not have, so
it could only pin "unbounded" as correct or fail forever. Body limits belong at
the ingress. And if the threat model includes hostile callers, the missing control
is *authentication* — this service has none at all — not a byte cap. What I did
instead: asserted that a fat-but-plausible 10 KB firmware string round-trips
intact, and wrote down where the real control belongs.

**3. Deep UI automation.** The dashboard is one static HTML file: no build step,
no framework, no router, no state beyond a 30-second `setInterval`. Page objects,
a cross-browser matrix and visual diffs would cost more to maintain than the rest
of the suite combined, to catch a class of bug — a JS typo, a renamed field — that
one smoke test already catches. One test, nightly, role/text selectors.

**4. Load testing with a real harness (k6/Locust).** The service is a single
uvicorn worker with a 40 ms artificial delay in front of it. A load harness
pointed at that measures the delay and the runner. I would build one against a
multi-worker deployment with resource limits and a load generator that is not on
the same box — none of which exists here.

**5. Mocking the scoring function in the API tests.** Tempting, and wrong: the
whole risk (R7) is that the *real* scoring constants change. A mocked scoring test
passes against every mutation of the thing it claims to protect. The API layer
computes its expected values from a second implementation written from the
published formula instead — so if someone edits the service's constants, the two
disagree and the tests go red. That is deliberate duplication, and the mutation
check is the evidence that it works.

**6. Asserting on FastAPI's validation error prose.** `status`, `loc` and `type`
are the contract; `msg` belongs to Pydantic and changes on minor upgrades.
Pinning it would buy upgrade toil and no signal.

---

## Gaps and what I would do next, in order

1. **Widen the net under recency.** `recency-inverted` is killed by only three
   tests, all in one file, and it is the most damaging realistic mutation. I
   would add cross-endpoint recency assertions to `/stations` and
   `/metrics/summary`, not just `/stations/{id}/status`.
2. **Second cover for the latest-per-station aggregation.**
   `metrics-errors-over-all-history` is killed by exactly one test. One test
   standing between the service and a silently wrong network-wide error total is
   one too few.
3. **Run the suite against PostgreSQL in-process, not only via e2e.** The
   `GROUP BY` semantics that produce R1 differ between engines, and today the
   fast layers only ever see SQLite. A `testcontainers` PostgreSQL fixture behind
   a marker would let the *whole* API layer run against both engines and would
   likely find more of R11.
4. **A stateful Hypothesis model of the ingest/read cycle.** The invariants are
   already written prose in `notes/risk-register.md`: distinct station IDs equals
   `total_stations`, the worklist equals the flagged subset, status always
   reflects `MAX(timestamp)`. A `RuleBasedStateMachine` would search sequences of
   reports for violations rather than checking the ones I thought of. This is the
   single highest-value addition and I ran out of time for it.
5. **Contract tests as a consumer-driven artifact.** Right now the contract layer
   validates against the schema the service publishes about itself, which cannot
   catch "the schema and the service are both wrong". A Pact-style contract owned
   by the dashboard would.
6. **Coverage of the `/health` gap (R8).** A readiness endpoint that touches the
   database is a service change, so I only documented it — but I would raise it
   as the first ticket, because it is the one defect here that makes a bad deploy
   invisible to the platform.

---

## Caveats on my own numbers

**Docker was not installed on the machine this suite was developed on.** The
local latency figures quoted here and in `notes/docker-vs-local.md` are measured;
the Docker-side effects are derived from the configuration and are stated as such.
The e2e, perf and UI layers were exercised against a live local uvicorn instance
(real HTTP, real serialisation, SQLite) and are written to run against
`docker compose` in CI; locally they skip with an actionable message rather than
failing. The one thing I have therefore *not* observed first-hand is the
latest-per-station join running against PostgreSQL, which is exactly the
difference the e2e job exists to cover — so I would watch that job's first run
before trusting it.
