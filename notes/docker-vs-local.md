# Docker vs local — why the two environments do not produce comparable numbers

The brief plants this: *"Docker and local environments behave differently. Understand why
before writing your performance tests."* There is one dominant answer and a long tail of
minor ones. Getting the ordering right is the whole point.

## The headline: Docker adds a hard 40 ms floor to every request

`docker-compose.yml:27` sets `SIMULATED_LATENCY_MS=40`. `service/app/main.py:20-27`
reads it once at import and, when non-zero, `await asyncio.sleep(0.040)` in an HTTP
middleware — **before** the route runs, on **every** request, including `/health` and
`/openapi.json`.

Locally the variable is unset, so `_LATENCY_MS == 0` and the middleware is a no-op.

Measured local baseline on this machine (uvicorn, SQLite, single worker, 200 requests
after warm-up):

| Endpoint | p50 | p95 |
|---|---|---|
| `GET /health` | 0.50 ms | 0.59 ms |
| `GET /metrics/summary` | 1.07 ms | 1.21 ms |
| `POST /reports` | 1.27 ms | 1.53 ms |

So the artificial delay is **~30× the entire local request cost**. Any latency budget is
really a budget on `SIMULATED_LATENCY_MS`, and a p95 assertion that passes locally at
5 ms will fail in Docker at 42 ms for reasons that have nothing to do with the code.
This is why the perf test in this suite reads its budget from `PERF_P95_BUDGET_MS`
(default 250 ms) and prints the measured numbers plus the environment, rather than
hardcoding a threshold.

## Everything else, ranked by how much it actually moves the number

| Difference | Local | Docker | Direction & rough magnitude |
|---|---|---|---|
| **Simulated latency** (`main.py:23`, `compose:27`) | 0 ms | 40 ms | **+40 ms on every request.** Dominates everything below |
| **Database** (`database.py:5`, `compose:26`) | SQLite file, in-process | PostgreSQL over the compose network | +0.3–2 ms per query, and a *different concurrency model*: SQLite serialises writers behind one file lock; PostgreSQL runs them concurrently. Throughput under concurrent writes is not comparable in either direction |
| **`--reload`** (brief's local command) | on | off (`Dockerfile:11`) | The reloader adds a file-watcher thread and re-execs on change; adds jitter and occasional multi-second stalls, not steady-state latency. **Local numbers taken with `--reload` are not trustworthy** — measure without it |
| **Worker count** | 1 (default) | 1 (`Dockerfile:11`, no `--workers`) | Same. Both are single-process, so neither can use more than one core; a concurrency test measures the *event loop plus threadpool*, not the machine |
| **Docker Desktop networking** | loopback | published port 8000 → VM → container | On macOS/Windows the VM boundary adds ~0.2–1 ms per request and caps small-request throughput. On Linux runners (GitHub Actions) this is near zero |
| **CPU/memory limits** | whole machine | **none set in compose** | No cgroup limits are declared, so the container gets the host's resources minus VM overhead. This is a gap, not a difference: nothing stops a noisy neighbour on a CI runner from skewing the run |
| **Base image / Python build** | host Python 3.11.4 (framework build) | `python:3.11-slim` (Debian) | Sub-millisecond differences in interpreter and libc. Irrelevant next to 40 ms |
| **Health checks** | n/a | **only the `db` service has one** (`compose:12-16`); `api` has none | Not a latency effect but a **test-harness** effect: `docker compose up` returns as soon as the api container *starts*, not when uvicorn is serving. Any e2e job must poll `/health` itself |
| **Volume / cold start** | warm `noc.db` in CWD | `db-data` volume; first boot runs initdb | First request after `compose up` is slower; also means **state survives `docker compose down`** unless you pass `-v` |
| **Logging** | uvicorn default to terminal | same, captured by the Docker log driver | Negligible; the json-file driver adds a write per request but it is buffered |

## What follows for the test suite

1. **No perf number is published without its environment.** The perf test emits
   `env=local|docker`, `SIMULATED_LATENCY_MS`, worker count and sample size next to
   p50/p95, and `TEST_STRATEGY.md` repeats the caveat.
2. **Perf never gates a merge.** With a 40 ms artificial floor and an unconstrained
   runner, a red perf job would be noise, not signal.
3. **Concurrency conclusions are environment-scoped.** The concurrent-write test asserts
   an invariant (no lost writes, no torn aggregate) rather than a throughput number,
   precisely because SQLite and PostgreSQL disagree on throughput and agree on the
   invariant.
4. **E2E startup uses a real readiness poll on `/health` with a timeout**, never a fixed
   sleep — the compose file gives the api container no healthcheck to wait on. Note that
   `/health` itself pays the 40 ms toll, so the poll budget accounts for it.

## The prediction, checked against a real Docker run

Docker was not installed on the machine this suite was developed on, so the Docker
column above was originally derived from the configuration rather than measured. The
first CI run on a GitHub-hosted runner settled it:

| Endpoint | local p95 | Docker p95 | delta |
|---|---|---|---|
| `GET /health` | 0.59 ms | 42.16 ms | +41.6 |
| `GET /metrics/summary` | 1.21 ms | 44.30 ms | +43.1 |
| `GET /stations` | — | 44.48 ms | — |
| `POST /reports` | 1.53 ms | 44.60 ms | +43.1 |

Every endpoint lands within ~1 ms of `local + 40 ms`, and `/health` — which does no
work at all — costs 42 ms. That is the simulated-latency middleware and nothing else:
the prediction that it dominates every other difference on this list holds, and the
remaining engine, networking and image differences are inside the noise. A p95 budget
measured locally would have been wrong by a factor of 30.
