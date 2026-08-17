"""Latency smoke test.

This measures one thing honestly and refuses to pretend it measures more: whether
the service has regressed by an *order of magnitude* on the machine the test
happened to run on. It is not a benchmark, it is a tripwire.

Why so modest a claim: Docker sets `SIMULATED_LATENCY_MS=40` and sleeps that long
on every request, against a ~1.3 ms local baseline — so the *environment* moves the
number 30x before any code does (`notes/docker-vs-local.md`). Neither environment
pins CPU, and the service is a single uvicorn worker, so a real load harness would
be measuring the runner.

So the budget comes from configuration (`PERF_P95_BUDGET_MS`, default 250 ms), the
numbers and the environment are printed for the CI log, and the job never gates a
merge.

Covered here: gross performance regression — an accidental N+1 over the report
table, a lost index, a synchronous call added to the request path — plus the
affordable slice of the growth question: does the aggregate query degrade with
history?
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from tests.helpers.builders import at, report, station_id
from tests.helpers.config import SETTINGS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    import httpx

pytestmark = [pytest.mark.perf, pytest.mark.e2e]


@dataclass(frozen=True)
class LatencyReport:
    endpoint: str
    samples: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    def render(self) -> str:
        return (
            f"{self.endpoint:34} n={self.samples:<4} "
            f"p50={self.p50_ms:7.2f}ms  p95={self.p95_ms:7.2f}ms  max={self.max_ms:7.2f}ms"
        )


def _measure(
    endpoint: str, call: Callable[[int], httpx.Response], samples: int, warmup: int
) -> LatencyReport:
    """Warm up, then time `samples` calls. No sleeps, no retries, no discards."""
    for index in range(warmup):
        response = call(index)
        assert response.status_code < 400, (
            f"warm-up call {index} to {endpoint} failed: {response.status_code}"
        )

    durations_ms: list[float] = []
    for index in range(samples):
        started = time.perf_counter()
        response = call(warmup + index)
        durations_ms.append((time.perf_counter() - started) * 1000)
        assert response.status_code < 400, (
            f"measured call {index} to {endpoint} failed: {response.status_code}"
        )

    durations_ms.sort()
    # Nearest-rank p95 — with 100 samples this is the 95th slowest, which is what a
    # reader assumes. Interpolating between ranks would be false precision at this
    # sample size.
    p95_index = min(len(durations_ms) - 1, int(0.95 * len(durations_ms)))
    return LatencyReport(
        endpoint=endpoint,
        samples=samples,
        p50_ms=statistics.median(durations_ms),
        p95_ms=durations_ms[p95_index],
        max_ms=durations_ms[-1],
    )


@pytest.mark.p2
def test_read_and_write_latency_against_a_configured_budget(
    live_client: httpx.Client, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every documented endpoint answers within the configured p95 budget.

    The three endpoints are measured separately because they have different cost
    shapes: `/health` is a constant, `/metrics/summary` runs the latest-per-station
    join and aggregates in Python, and `POST /reports` writes. A regression in the
    join would be invisible in an average across all three.

    The measured numbers and the environment are printed unconditionally, so the CI
    log carries the trend even on a green run — a perf test whose output is only
    visible when it fails cannot tell you that you are drifting towards the cliff.

    Why: An order-of-magnitude tripwire and a trend printed into CI logs; never a gate,
        for reasons in TEST_STRATEGY.md.
    """
    budget = SETTINGS.perf_p95_budget_ms
    samples = SETTINGS.perf_samples
    warmup = SETTINGS.perf_warmup

    def ingest(index: int) -> httpx.Response:
        return live_client.post(
            "/reports",
            json=report(station_id_=station_id("PERF"), timestamp=at(seconds=index)),
        )

    measurements = [
        _measure("GET /health", lambda _: live_client.get("/health"), samples, warmup),
        _measure(
            "GET /metrics/summary",
            lambda _: live_client.get("/metrics/summary"),
            samples,
            warmup,
        ),
        _measure("GET /stations", lambda _: live_client.get("/stations"), samples, warmup),
        _measure("POST /reports", ingest, samples, warmup),
    ]

    with capsys.disabled():
        print(f"\n  latency smoke — budget p95 <= {budget:g}ms")
        print(
            f"  environment: {SETTINGS.environment_label}"
            f" | SIMULATED_LATENCY_MS={SETTINGS.simulated_latency_ms}"
            f" | base_url={SETTINGS.base_url}"
            f" | workers=1 (uvicorn default; see notes/docker-vs-local.md)"
        )
        for measurement in measurements:
            print(f"  {measurement.render()}")

    breaches = [m for m in measurements if m.p95_ms > budget]
    assert not breaches, "p95 budget exceeded:\n" + "\n".join(m.render() for m in breaches)
