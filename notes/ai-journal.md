# AI journal

Raw log of every prompt sent to the assistant (Claude Code / Opus, in VS Code) plus the
raw output, recorded **before** I edited anything. `AI_USAGE.md` is assembled from this
file. Entries are append-only and in chronological order.

Convention:
- `PROMPT` — exactly what I sent.
- `RAW OUTPUT` — what came back, unedited (trimmed only where marked `[…]`).
- `WHAT I DID WITH IT` — accepted / modified / rejected, and why.

---

## Entry 0 — before any prompt (no AI)

Did this by hand, deliberately, before opening a session:

```bash
python3 -m venv .venv && .venv/bin/pip install -r service/requirements.txt
cd service && DATABASE_URL="sqlite:///./explore.db" ../.venv/bin/uvicorn app.main:app --port 8000
```

Then curl'd every endpoint in the brief's table, plus `/openapi.json`. Raw session output
is captured in `notes/behaviour-inventory.md`. Two things I would not have learned from an
LLM summary:

1. The brief and the playbook I was working from both describe this as an **in-memory**
   service. It is not — `service/app/database.py` is SQLAlchemy over SQLite (local) or
   PostgreSQL (Docker). Every "how do I isolate an in-memory store" answer is therefore
   the wrong question. This single fact changed the whole isolation design.
2. `POST /reports` is **append-only**. It never updates a station row; it inserts a
   report row and every read endpoint recomputes "latest per station" with a
   `GROUP BY station_id, MAX(timestamp)` join. Everything interesting about this service —
   duplicates, clock skew, out-of-order arrival — falls out of that one design choice.

Standing context block from the playbook was placed in `CLAUDE.md` at repo root so it is
in context for every session.

---

## Entry 1 — Prompt 2a, behaviour inventory

**PROMPT**

```
Read every file under service/ and produce a behaviour inventory as a markdown
table. Do not write any tests yet.

Columns: endpoint/function | inputs & validation | documented behaviour |
actual behaviour in code | where they disagree.

Then, separately, answer these specifically:
1. Exactly how is the hygiene score computed? Give me the formula, every
   threshold/constant, and every branch.
2. Where is state stored? What is its lifetime? What resets it?
3. What happens on: duplicate station_id, out-of-order timestamps, a report
   with a timestamp in the future, negative latency_ms, negative error_count,
   an unknown connectivity_status, a missing field, an extra field, an
   oversized payload?
4. Is there any concurrency handling on the write path?
5. What is the boundary condition for a station being flagged "poor hygiene" —
   is it inclusive or exclusive, and does the code match any docstring/README?
6. Are there endpoints or fields in the code that the assignment brief doesn't
   mention?

Cite file and line for each answer.
```

**RAW OUTPUT** — landed verbatim in `notes/behaviour-inventory.md` (kept as a separate
file because it is long and I keep referring back to it).

**WHAT I DID WITH IT — modified.** The static read was accurate on the code but wrong on
two behaviours, both of which I only caught because I had already curl'd the service:

- It claimed duplicate `(station_id, timestamp)` pairs were "harmless, the join returns
  the latest row". They are not harmless — the join returns **both** rows, so the station
  appears twice in `GET /stations` and is counted twice in `GET /metrics/summary`.
  Verified by hand (`DUP-1`, entry in the inventory). This became risk R1.
- It reported the `max(score, 0.0)` clamp in `scoring.py:37` as a live floor. Maximum
  total penalty is 40+30+20 = 90, so the score can never go below 10 and the clamp is
  **unreachable**. Verified: worst-case payload returns `10.0`, not `0.0`.

I corrected both in the committed inventory and added the curl evidence under each.

---

## Entry 2 — Prompt 2b, the Docker hint

**PROMPT**

```
Compare the Docker setup (Dockerfile, docker-compose.yml) with the documented
local run command. List every difference that could affect measured latency or
throughput — resource limits, worker count, reload mode, base image, health
checks, port mapping, logging. For each, state the direction and rough
magnitude of the effect on a latency benchmark.
```

**RAW OUTPUT** — in `notes/docker-vs-local.md`.

**WHAT I DID WITH IT — accepted the table, rewrote the conclusion.** The comparison of
worker count / reload / base image was right but it buried the actual answer under six
minor rows. The hint in the brief has exactly one dominant answer:
`docker-compose.yml:27` sets `SIMULATED_LATENCY_MS=40`, and
`service/app/main.py:23-27` sleeps that long in middleware **on every request including
`/health`**. Docker is a hard 40 ms floor; local is 0. Nothing else on the list moves the
number by more than a few ms. I reordered so that is the headline and the rest is noise,
and I promoted it to the perf caveat in `TEST_STRATEGY.md`.

---

## Entry 3 — Prompt 2c, risk register

**PROMPT**

```
Based on the inventory, draft a risk register for this service as a table:
failure mode | how it would reach production | blast radius for an EV charging
network operator | likelihood | which test layer should catch it | cost of
catching it there.

Rank by (blast radius x likelihood) / detection cost. Be concrete about the
domain: a wrong hygiene score means a field tech is dispatched to a healthy
station, or a dead station is never flagged.

Give me your ranking, then tell me which three risks you would NOT spend
automation budget on and why.
```

**RAW OUTPUT** — first draft is preserved in `notes/risk-register.md` under
"AI first-draft ranking (superseded)".

**WHAT I DID WITH IT — heavily edited; this is the artifact I care most about.** Changes:

- It ranked "negative `latency_ms` accepted" as a top-5 risk. It is not a risk at all:
  `schemas.py:11` has `ge=0`, Pydantic rejects it with 422, and I had already confirmed
  that with curl. Deleted.
- It missed the highest-severity finding entirely: an offline station reporting
  `latency_ms=0, error_count=0` scores **exactly 60.0**, and the flag test is
  `score < 60` (`scoring.py:41`), so a **completely dead station is never flagged**.
  That is precisely the "dead station is never flagged" harm the prompt asked about, and
  it came out of my own boundary arithmetic, not the model's. Added as R2.
- It missed that the error penalty caps at −30, so a station reporting 100,000 errors
  while online scores 70 and is invisible. Added as R4. Verified with curl (`CAP-ERR`).
- It proposed "test that the DB rolls back on error" — there is no error path that could
  roll back; `reports.py:30-31` is a single add+commit with no exception handling.
  Dropped.

The ranking, the three not-worth-automating rows, and all the blast-radius wording in the
committed version are mine.

---

## Entry 4 — Phase 2, decide (no AI)

Deliberately no prompt here. Layer weighting, the in-process vs live-HTTP split, and the
isolation decision are written up in `TEST_STRATEGY.md` in my own words. The one thing I
wanted from a model — "which isolation strategy should I use for an in-memory store" —
would have produced a confidently wrong answer, because the premise is wrong (see
Entry 0). Asking it would have cost me an hour.

---

## Entry 5 — Phase 3, scaffold

**PROMPT** (playbook §4, verbatim — the structure/marker/fixture/Makefile block, ending
with "Explain the isolation options first. Don't write the whole thing yet.")

**RAW OUTPUT — isolation options offered:**

> **Option A — unique station IDs per test (namespacing).** Each test generates
> `STATION-{uuid4}`. No reset needed, works against a live container, parallel-safe.
> Trade-off: aggregate endpoints (`/metrics/summary`) see every other test's rows, so you
> can never assert `total_stations == 3`.
>
> **Option B — restart the app instance per test.** A fixture that re-imports
> `app.main` against a fresh SQLite file. Perfect isolation, exact aggregate assertions.
> Trade-off: ~200 ms per test for `create_all` plus module re-import; cannot work against
> a live container.
>
> **Option C — truncate tables between tests.** A fixture that issues
> `DELETE FROM station_reports` via SQLAlchemy after each test. Fast (~1 ms), exact
> aggregates. Trade-off: serial-only — two tests running in parallel will delete each
> other's rows.

**WHAT I DID WITH IT — modified: took none of the three as-is.** All three assume the
tests must share the service's own engine. They do not. FastAPI exposes `get_db` as a
dependency (`database.py:15`), so `app.dependency_overrides[get_db]` lets each test bind
the app to its **own** engine for the duration of that test — perfect isolation, exact
aggregate assertions, no truncation, no re-import, parallel-safe, and zero changes to
`service/`. That is Option D and it is what `tests/conftest.py::api_client` implements.

Options A–C were not wasted: Option A is exactly right for the one layer where
dependency overrides are impossible — e2e against a live container over real HTTP — so
the suite uses **D in-process and A over the wire**, and `TEST_STRATEGY.md` says why.
Full before/after is Example 2 in `AI_USAGE.md`.

---

## Entry 6 — Phase 4, layer by layer

Separate prompts per layer, per the playbook. Notable interventions:

- **Unit/boundary.** Asked for boundary tests driven by risk rows R2/R3. Output was a
  clean `parametrize` table; I added the `DEAD-STATION` case (offline/0/0 → 60.0 → not
  flagged) which it had not generated, because that is the whole point of the boundary.
- **Property-based.** It proposed "score is monotonically decreasing in `error_count`" as
  a strict property. Wrong: the penalty caps at −30, so beyond 6 errors the score is
  flat. The correct invariant is **non-increasing**, and there is a second, sharper one it
  missed — the score is *constant* above the cap, which is the R4 risk expressed as a
  property. Both are in `tests/unit/test_scoring_properties.py`.
- **Contract.** It wrote tests validating 200 responses against `/openapi.json` and
  stopped there. The interesting contract finding is the opposite direction: the code
  raises 404 at `stations.py:56` but the schema for that path documents only 200 and 422.
  A generated client will not handle the 404. Added as an `xfail(strict=True)`.
- **Concurrency.** Rejected outright. See Example 3 in `AI_USAGE.md`.

---

## Entry 7 — mutation check (no AI)

Ran the mutation check by hand against a scratch copy of `service/` rather than trusting
the suite's own green. Results, and which mutant each test caught, are in
`TEST_STRATEGY.md` §"Did I actually test anything?".
