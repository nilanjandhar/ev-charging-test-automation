# Behaviour inventory — NOC Station Health API

Produced during recon (Phase 1), before any test code. Every row was checked against a
running instance, not just read. Local run:
`DATABASE_URL="sqlite:///./explore.db" uvicorn app.main:app --port 8000`.

## 1. Endpoint inventory

| Endpoint / function | Inputs & validation | Documented behaviour | Actual behaviour in code | Where they disagree |
|---|---|---|---|---|
| `POST /reports` (`routers/reports.py:11`) | `ReportIn` (`schemas.py:6-12`): `station_id` str min_len 1; `timestamp` datetime; `connectivity_status` `Literal["online","offline"]`; `latency_ms` float ≥0; `error_count` int ≥0; `firmware_version` str min_len 1 | "Ingest a station health report" | Computes score, **inserts a new row** (`reports.py:30-31`), returns 201 `{station_id, hygiene_score, flagged}` | Brief implies *the* station record is updated. It is **append-only** — nothing is ever updated or deduplicated. Extra JSON fields are silently ignored (Pydantic default `extra="ignore"`); numeric strings are coerced (`"120"` → `120.0`, lax mode) |
| `GET /stations` (`stations.py:12`) | none | "List all known stations with latest status" | `GROUP BY station_id, MAX(timestamp)` subquery joined back to rows (`stations.py:15-33`), ordered by `station_id` | **Returns one row per (station, tied-max-timestamp) pair, not per station.** Duplicate timestamps ⇒ the same station appears more than once. See R1 |
| `GET /stations/{station_id}/status` (`stations.py:47`) | path param, no validation | "Latest status for a specific station" | `ORDER BY timestamp DESC LIMIT 1` (`stations.py:52-54`); 404 with `{"detail": "Station '<id>' not found"}` (`stations.py:56`) | The 404 is **not in the OpenAPI schema** — that path documents only 200 and 422. Also: ties are broken arbitrarily here (`LIMIT 1`) but *not* collapsed in `/stations`, so the two endpoints disagree under duplicates |
| `GET /stations/poor-hygiene` (`stations.py:70`) | none | "All currently flagged stations" | Same latest-per-station join, `WHERE flagged = true` (`stations.py:89`); returns `{station_id, hygiene_score, latest_timestamp}` | **No `ORDER BY`** — result order is whatever the DB returns, so it differs between SQLite and PostgreSQL. Also inherits the duplicate-row defect |
| `GET /metrics/summary` (`metrics.py:11`) | none | "Aggregated network metrics" | Aggregates over the same latest-per-station join (`metrics.py:33-40`); `average_latency_ms` is `None` when there are no stations | `total_stations` is a **row count, not a distinct-station count** (R1). `average_latency_ms` is an unweighted mean with no upper bound on the input — one absurd report poisons it (R5) |
| `GET /health` (`main.py:43`) | none | "Health check" | Returns `{"status":"ok"}` unconditionally | **Does not check the database.** It returns 200 with a dead DB, so it is a liveness probe masquerading as a readiness probe. It is also delayed by the simulated-latency middleware |
| `GET /` (`main.py:38`) | none | "Dashboard UI" | Serves `static/index.html`; registered with `include_in_schema=False` | Undocumented in OpenAPI by design |
| `compute_hygiene_score` (`scoring.py:21`) | — | Formula in `service/README.md:61-69` and the module docstring | See §2 | Code matches both docs exactly. The **design** is the problem, not a doc mismatch |

## 2. How the score is computed (`service/app/scoring.py`)

```
score = 100.0
if connectivity_status == "offline":  score -= 40.0          # OFFLINE_PENALTY,      :14
score -= min(error_count * 5.0, 30.0)                        # ERROR_PENALTY_*,      :15-16
score -= min(latency_ms / 20.0, 20.0)                        # LATENCY_*,            :17-18
return round(max(score, 0.0), 2)                             #                       :37

is_flagged(score) -> score < 60.0                            # FLAGGING_THRESHOLD,   :12, :41
```

Constants: `FLAGGING_THRESHOLD=60.0`, `OFFLINE_PENALTY=40.0`, `ERROR_PENALTY_PER=5.0`,
`ERROR_PENALTY_CAP=30.0`, `LATENCY_DIVISOR=20.0`, `LATENCY_PENALTY_CAP=20.0`.

Branches: exactly one (`offline`). Both penalties are `min()` caps, not branches.

Derived facts that matter for testing:

- **Range is [10, 100], not [0, 100].** Max total penalty is 40+30+20 = 90, so
  `max(score, 0.0)` at `scoring.py:37` is **unreachable dead code**.
  Verified: offline + `latency_ms=100000` + `error_count=100000` → `10.0`.
- **Error penalty saturates at 6 errors** (6 × 5 = 30). 6 errors and 100,000 errors score
  identically. Verified: online/0 ms/100,000 errors → `70.0`, `flagged=false`.
- **Latency penalty saturates at 400 ms.** 400 ms and 10^308 ms score identically.
- **An online station with 0 errors can never be flagged**: best case penalty is 20, so
  the floor for `online` + 0 errors is 80.
- **Flagging is only reachable three ways**: offline with any latency > 0 or any error;
  or online with ≥ 5 errors and high latency. Exhaustively: `offline` needs a further
  0.01 penalty from somewhere; `online` needs error+latency penalties summing > 40.

## 3. Where state lives

`service/app/database.py:5-12`. SQLAlchemy engine built at **import time** from
`DATABASE_URL`, default `sqlite:///./noc.db` (a file, relative to CWD). Docker overrides
it to PostgreSQL (`docker-compose.yml:26`). `Base.metadata.create_all` runs at import of
`app.main` (`main.py:10`).

Lifetime: **the file / the PostgreSQL volume**, not the process. Restarting the service
does *not* clear state — the local SQLite file and the compose `db-data` volume both
survive. Nothing in the service resets it; there is no delete or reset endpoint. The only
resets are `rm noc.db` locally and `docker compose down -v` in Docker.

> This is the fact the playbook I was working from got wrong (it assumed an in-memory
> store). Test isolation is therefore a database problem, not a process problem —
> see `TEST_STRATEGY.md` §"Test data & isolation".

`get_db` (`database.py:15-20`) is a FastAPI dependency, which is the hook the test suite
uses to swap the engine per test without touching `service/`.

## 4. Edge-case behaviour (all verified with curl)

| Input | Result | Evidence |
|---|---|---|
| **Duplicate `(station_id, timestamp)`** — the same report sent twice, i.e. any at-least-once retry | **201 twice.** `GET /stations` lists the station **twice**; `/metrics/summary` counts it twice (`total_stations` 3 for 2 real stations); `/stations/{id}/status` still returns one | `DUP-1`: `len(/stations)=16, distinct=15, total_stations=16` |
| **Out-of-order arrival** (older timestamp POSTed second) | Correct — latest *by timestamp* wins regardless of arrival order | `OOO`: 12:00 offline posted first, 09:00 online posted second → status stays offline/10.0/flagged |
| **Future timestamp** | Accepted, no validation. The future report **permanently pins** the station: every later real report loses the `MAX(timestamp)` comparison | `CLOCK-SKEW`: 2099 online/100 posted, then 2024 offline/900 ms/50 errors (score 10, flagged) → status still reports `2099-01-01`, `online`, `100.0`, `flagged=false` |
| **Same instant, different UTC offsets** | Offsets are **dropped, not normalised**. `12:00+02:00` is stored as naive `12:00` and beats `10:00Z`, which is the same instant | `TZ`: `10:00Z` (offline, flagged) then `12:00+02:00` (online) → the `+02:00` report wins |
| **Timestamp round-trip** | `2024-06-01T10:00:00Z` in → `2024-06-01T10:00:00` out. The column is naive `DateTime` (`models.py:11`), so the client cannot tell what zone it is reading | every response above |
| Negative `latency_ms` | 422, `type: "greater_than_equal"`, `loc: ["body","latency_ms"]` | |
| Negative `error_count` | 422, `type: "greater_than_equal"` | |
| Unknown `connectivity_status` (`"degraded"`, `"ONLINE"`) | 422, `type: "literal_error"`, msg `Input should be 'online' or 'offline'` — **case-sensitive** | |
| Missing field | 422, `type: "missing"` | |
| **Extra field** | **201, silently ignored.** Sending `"hygiene_score": 0` does *not* override the computed score — no mass-assignment | `EXTRA` → 90.0 |
| Numeric strings (`"120"`, `"2"`) | **201, coerced.** Pydantic lax mode | `STR-NUM` → 84.0 |
| Float `error_count` (2.7) | 422, `type: "int_from_float"` | |
| Malformed JSON body | 422, `type: "json_invalid"` (not 400) | |
| Body with no `Content-Type` | 422, `type: "model_attributes_type"` | |
| **Oversized payload** | **No limit anywhere.** No middleware, no `max_length` on `station_id` / `firmware_version`, no uvicorn body cap configured. A 10 MB `firmware_version` is accepted and stored | |
| `1e308` for `latency_ms` | 201, score 80 (penalty capped) — and it drags `average_latency_ms` to `6.25e+306` for the whole network | `INF` |
| `GET /reports` | 405 | |
| `POST /reports/` (trailing slash) | 307 redirect | |

## 5. Concurrency on the write path

None, and none needed for correctness of the write itself. `ingest_report`
(`reports.py:12-33`) is a synchronous endpoint, so FastAPI runs it in a threadpool; each
request gets its own `Session` (`database.py:16`) and does a single `add` + `commit` with
no read-modify-write. Because the table is **append-only there is no lost-update window**.

Verified: 50 concurrent POSTs for one station → 50 × 201, final status = highest
timestamp, no errors, no lost rows.

The risk is on the *read* path instead: `/metrics/summary` issues one query but computes
six aggregates in Python (`metrics.py:33-40`), so a report landing mid-request cannot tear
the result. There is no transaction spanning endpoints, so cross-endpoint reads
(`/stations` then `/metrics/summary`) can observe different snapshots — acceptable for a
dashboard, but it means concurrency tests must assert *valid serialisation*, not equality.

Environment caveat: SQLite serialises writers with a file lock (5 s busy timeout, single
uvicorn worker). PostgreSQL under Docker does not. **Concurrency results are not portable
between the two**, which is a second reading of the brief's "Docker and local behave
differently" hint.

## 6. Things in the code the brief does not mention

- `SIMULATED_LATENCY_MS` env var + the `simulated_latency_middleware` (`main.py:18-27`) —
  sleeps on **every** request including `/health`. Docker sets it to 40 ms
  (`docker-compose.yml:27`); local defaults to 0. This is the answer to the perf hint.
- `DATABASE_URL` env var (`database.py:5`) and the whole SQLite-vs-PostgreSQL split. The
  candidate-facing brief says nothing about a database at all.
- `/static` mount, `/docs`, `/redoc`, `/openapi.json`.
- `firmware_version` is required, stored, and returned by `/stations/{id}/status`, but is
  **used by nothing** — not in scoring, not in metrics. There is no firmware-drift metric
  even though firmware is the most common real cause of a fleet-wide hygiene regression.
- `created_at` (`models.py:18`) is stored but never exposed by any endpoint — so a client
  cannot distinguish "reported at" from "received at", which is exactly what you would
  need to detect the clock-skew case above.
