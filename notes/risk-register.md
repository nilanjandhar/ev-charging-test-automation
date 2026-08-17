# Risk register — NOC Station Health API

The backbone of `TEST_STRATEGY.md`. Every test in this suite maps to a row here; if a
test does not map to a row, it should not exist.

Scoring: **blast radius** and **likelihood** are 1–5. **Detection cost** is 1–5 (1 =
a parametrized unit case, 5 = a containerised load harness). Priority =
(blast × likelihood) ÷ cost. Ranked.

The domain translation I keep coming back to: a hygiene score that is wrong *low* sends
a field tech 90 minutes to a healthy charger. A hygiene score that is wrong *high* leaves
a dead charger on a highway corridor, and the first person to notice is a driver at 20%
battery.

## Ranked register

| # | Failure mode | How it reaches production | Blast radius for a network operator | L | B | Layer that should catch it | Cost | Pri |
|---|---|---|---|---|---|---|---|---|
| **R2** | **A fully offline station is never flagged.** Offline + `latency_ms=0` + `error_count=0` scores **exactly 60.0**, and the flag test is `score < 60` (`scoring.py:41`) | Already shipped. It is the specified behaviour — `service/README.md:69` says "falls below 60" and the code agrees. Nobody wrote the test that asks "what does a *dead* station score?" | Maximum. A station that has stopped talking is the single most important thing this service exists to surface, and it is the one case that cannot appear on `/stations/poor-hygiene`. Silent outage until a driver complains | 5 | 5 | Unit — boundary table on `is_flagged` / `compute_hygiene_score` | 1 | **25** |
| **R1** | **A retried report double-counts the station.** Duplicate `(station_id, timestamp)` rows both survive the `MAX(timestamp)` join (`stations.py:26-30`, `metrics.py:24-30`) | Any at-least-once delivery: an edge gateway retry, a station re-sending after a TCP reset, a replayed Kafka partition. Requires **zero** code change to trigger | Every network-level number is wrong and *silently* wrong. `total_stations`, `online_count`, `flagged_count`, `total_error_count` and `average_latency_ms` all inflate. Capacity planning and SLA reporting run off these | 5 | 4 | API integration — cross-endpoint consistency (`len(/stations) == distinct station_ids == total_stations`) | 2 | **10** |
| **R3** | **One clock-skewed station pins itself forever.** A report timestamped in the future always wins `MAX(timestamp)`, so every subsequent real report is invisible (`stations.py:52`) | A station with a dead RTC or a bad NTP sync reports 2099. No validation rejects it (`schemas.py:8`) | That station's status freezes at whatever it last claimed. If it froze while "online, healthy", it is permanently green and permanently unmonitorable — a worse version of R2 because it also looks fine | 4 | 5 | API integration — recency semantics | 2 | **10** |
| **R16** | **The service violates its own published `format: date-time`.** `latest_timestamp` is declared RFC 3339 in the schema; `models.py:11` is a naive `DateTime`, so it serialises `2024-06-01T10:00:00` with no offset | Found by schemathesis, not by reading code — `jsonschema` ignores `format` by default, schemathesis validates it. Present on all three station endpoints | A strict generated client rejects the response; a lenient one parses it as browser-local time, which is what the dashboard does (`static/index.html:108`), so an operator in Los Angeles reads every "last report" time shifted by eight hours. Same root cause as R6 | 4 | 3 | Contract — schema conformance with format checking enabled | 1 | **12** |
| **R7** | **Scoring constant / threshold regression.** Someone tunes `ERROR_PENALTY_PER`, `LATENCY_DIVISOR` or `FLAGGING_THRESHOLD` (`scoring.py:12-18`) and the blast lands on the whole fleet at once | Routine "let's make the score more sensitive" ticket. Nothing today would go red | Fleet-wide. Every station re-scores simultaneously: either a dispatch storm to healthy sites, or a fleet that quietly stops flagging | 3 | 5 | Unit — exact boundary values at, just below, just above each threshold | 1 | **15** |
| **R4** | **The error penalty caps at −30, so catastrophic error rates are invisible.** 6 errors and 100,000 errors score identically; online + 100,000 errors = **70.0, not flagged** | Shipped behaviour, and the cap is deliberate — but nothing documents that a station in a hard error loop stays green | A charger failing every session reports thousands of errors and never appears on the worklist. Revenue loss plus warranty exposure, invisible to the NOC | 3 | 4 | Property-based — "score is constant above the cap" states the risk as an invariant | 2 | **6** |
| **R6** | **UTC offsets are dropped, not normalised.** The column is naive `DateTime` (`models.py:11`); `12:00+02:00` outranks `10:00Z`, the same instant | Any station outside UTC, or any gateway that stamps local time. Guaranteed the moment the network crosses a timezone | Recency is wrong by up to 14 hours in either direction. A Berlin station's stale report beats a London station's fresh one; `latest_timestamp` comes back with no zone at all, so no client can correct it | 4 | 3 | API integration — timestamp semantics, plus a contract assertion on round-trip fidelity | 2 | **6** |
| **R5** | **One absurd report poisons `average_latency_ms` for the entire network.** `latency_ms` has no upper bound (`schemas.py:11`) and the metric is an unweighted mean (`metrics.py:38-40`) | A sensor glitch or a unit mix-up (seconds sent as ms). Observed: one report of `1e308` moved the network average to `6.25e+306` | The network latency KPI becomes meaningless, and it is exactly the number an ops lead watches on the dashboard. Also poisons any alert threshold built on it | 3 | 3 | API integration — metrics robustness | 2 | **4.5** |
| **R11** | **The three read endpoints can disagree.** `/stations`, `/stations/{id}/status` and `/stations/poor-hygiene` each recompute "latest" with different SQL; ties are collapsed by `LIMIT 1` in one and not in the others | Present today (it is the mechanism behind R1). Widens whenever anyone touches one query without the others | A station appears flagged on the dashboard list but healthy on its own detail page. Operators stop trusting the tool — the most expensive failure mode there is, because it is unrecoverable by a hotfix | 4 | 3 | API integration — cross-endpoint consistency | 2 | **6** |
| **R15** | **Rounding decides the flag at the boundary, and widens R2's blind spot.** `scoring.py:37` rounds the *final* score to 2 dp with half-even binary rounding, so a penalty under 0.005 disappears entirely | Found by Hypothesis, not by reading the code, while falsifying "any non-zero latency flags an offline station". An offline station reporting **any latency below 0.1 ms** with no errors scores exactly 60.0 and is not flagged. The offline penalty is also 40 ± 0.01 rather than exactly 40, depending on where the fraction lands | Small in isolation — 0.01 of a score point. It matters because it sits precisely on the dispatch boundary and it turns R2 from "one pristine corner case" into "every offline station whose latency reading is a stale zero-ish value". Nothing about truck-roll decisions should be settled by IEEE-754 tie-breaking | 3 | 2 | Property-based — this is exactly the class of bug examples do not find | 1 | **6** |
| **R9** | **`/stations/{id}/status` returns a 404 the OpenAPI schema does not document** (`stations.py:56` vs the schema's `200`/`422` for that path) | Present today. A generated client or a strict gateway sees an undocumented status | Client-side crash rather than a handled "unknown station". Small blast, but free to catch | 3 | 2 | Contract — schema conformance including error responses | 1 | **6** |
| **R8** | **`/health` does not check the database** (`main.py:43-45`) | Present today. It returns `{"status":"ok"}` with the DB gone | Kubernetes and the load balancer both believe a service that 500s on every read is healthy. Rollouts proceed into a broken deploy; the outage is invisible to the platform | 3 | 3 | E2E / contract — assert what `/health` actually proves, and say in the strategy doc that it is a liveness probe, not readiness | 2 | **4.5** |
| **R10** | **`/stations/poor-hygiene` has no `ORDER BY`** (`stations.py:82-91`) | Present today. Order is whatever the engine returns, and it differs between SQLite and PostgreSQL | The worklist reshuffles between refreshes; there is no pagination, so "the top of the list" is not a stable concept for a triaging operator | 2 | 2 | API integration — assert set semantics, not order (and flag the missing order as a defect rather than pinning today's accident) | 1 | **4** |
| **R13** | Unbounded table growth. Every read is a full `GROUP BY` over all history; there is no retention, index on `timestamp`, or archival | Time. 500 stations × 1 report/minute ≈ 260 M rows/year | Read latency degrades continuously until the dashboard times out. Slow, predictable, and nobody notices until it is bad | 3 | 3 | **Not automated here** — see below | 5 | 1.8 |
| **R12** | No payload-size limit anywhere: no middleware, no `max_length` on `station_id` / `firmware_version`, no uvicorn body cap | An unauthenticated caller, or a station with a corrupt firmware string | Memory and storage exhaustion. But the service has no auth at all, so this is not the interesting attack | 2 | 3 | **Not automated here** — see below | 4 | 1.5 |
| **R14** | Dashboard fails silently-ish: `static/index.html:135-138` catches fetch errors into a small red line and leaves the last-rendered numbers on screen | Any API blip | An operator reads stale numbers as current. Real, but one smoke test is the sensible ceiling | 2 | 2 | UI smoke — one Playwright test | 3 | 1.3 |

## The three I would not spend automation budget on

**1. R13 — data-volume / query-degradation testing.** The failure is real and I would
raise it as a ticket, but automating it means seeding tens of millions of rows and
measuring query plans. Against SQLite on a laptop or a GitHub runner, that measures the
runner's disk, not the service. The honest version of this test needs production-shaped
data on production-shaped hardware; it belongs in a staging soak with query-time
dashboards, not in a PR gate. **What I do instead:** document the growth math above so the
number is on the record.

**2. R12 — payload-size / resource-exhaustion testing.** A test that POSTs a 100 MB body
asserts a limit this service does not have, so it would either fail forever or pin
"unbounded" as correct. Body limits belong at the ingress (nginx/Envoy/API gateway),
which is not in this repo, and the service has no authentication either — so if the threat
model includes hostile callers, the missing control is authentication, not a byte cap.
**What I do instead:** one row in the known-issues section naming where the control
belongs.

**3. Deep UI automation.** The dashboard is one static HTML file, no build step, no
framework, no router, no state beyond a 30-second `setInterval`. Page-object
infrastructure, cross-browser matrices and visual diffs would cost more to maintain than
the whole rest of the suite, to catch a class of bug (a JS typo) that a single smoke test
already catches. **What I do instead:** exactly one Playwright test, role/text selectors,
nightly only.

Honourable mention — **asserting on FastAPI's validation error prose.** I assert on
`status == 422` and on the machine-readable `type` / `loc` fields, never on `msg`. Those
strings belong to Pydantic and change on minor upgrades; pinning them buys nothing and
generates upgrade toil.

## First-pass ranking (superseded by verification against the running service)

Kept for the record, because the corrections are the point:

1. ~~Negative `latency_ms` / `error_count` accepted and scored~~ — **wrong**. `schemas.py:11-12`
   has `ge=0`; both return 422. Removed entirely. This is what static reading of code
   gets you when you skip standing the service up.
2. ~~Score can go negative / clamp at 0 matters~~ — **unreachable**. Max penalty is 90, so
   the floor is 10 and `max(score, 0.0)` (`scoring.py:37`) is dead code. Kept, but demoted
   to a one-line note rather than a risk.
3. ~~Lost updates on concurrent writes to the same station~~ — **not possible**. The write
   path is a single append with no read-modify-write (`reports.py:30-31`); 50 concurrent
   POSTs produced 50 rows and the correct final status. The concurrency test that survives
   asserts *valid serialisation and untorn aggregates*, which is a different claim.
4. ~~Transaction rollback on ingest failure~~ — there is no failure path to roll back.
5. Duplicate reports "are harmless, the join picks the latest" — **wrong, and it is now
   R1**, the second-highest risk in the register. The join returns *both* tied rows.

Every one of those corrections came from curl output, not from re-reading the source.
