"""Environment-driven configuration.

Nothing in a test body may hardcode a host, a port, or a budget. Every knob
lives here with a default that works on a clean clone, so `pytest` works with no
setup and CI can retarget the suite with environment variables alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


@dataclass(frozen=True)
class Settings:
    """Resolved once per session; frozen so a test cannot mutate it for the next one."""

    #: Live service under test, used by the e2e / perf / ui layers only.
    base_url: str
    #: How long to wait for that service to answer /health before skipping.
    readiness_timeout_s: float
    #: Latency smoke: sample size and the p95 budget it is judged against.
    perf_samples: int
    perf_warmup: int
    perf_p95_budget_ms: float
    #: Concurrency smoke: how many simultaneous writers.
    concurrency_writers: int
    #: Schemathesis example budget. Small in the PR gate, large nightly.
    schemathesis_max_examples: int
    #: "ci" (derandomised, bounded) or "nightly" (broad). See tests/conftest.py.
    hypothesis_profile: str
    #: Recorded alongside any published performance number.
    environment_label: str
    simulated_latency_ms: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            base_url=_env_str("BASE_URL", "http://localhost:8000").rstrip("/"),
            readiness_timeout_s=_env_float("READINESS_TIMEOUT_S", 30.0),
            perf_samples=_env_int("PERF_SAMPLES", 100),
            perf_warmup=_env_int("PERF_WARMUP", 20),
            # 250 ms is deliberately generous: Docker adds a hard 40 ms floor via
            # SIMULATED_LATENCY_MS and CI runners are noisy neighbours. This budget
            # catches an order-of-magnitude regression, which is all a smoke test
            # on shared hardware can honestly claim. See notes/docker-vs-local.md.
            perf_p95_budget_ms=_env_float("PERF_P95_BUDGET_MS", 250.0),
            concurrency_writers=_env_int("CONCURRENCY_WRITERS", 25),
            schemathesis_max_examples=_env_int("SCHEMATHESIS_MAX_EXAMPLES", 15),
            hypothesis_profile=_env_str("HYPOTHESIS_PROFILE", "ci"),
            environment_label=_env_str("TEST_ENV_LABEL", "local"),
            simulated_latency_ms=_env_int("SIMULATED_LATENCY_MS", 0),
        )


SETTINGS = Settings.from_env()
