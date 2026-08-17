"""Client construction and readiness probing.

`starlette.testclient.TestClient` is itself an `httpx.Client` subclass, so the
in-process client and the live-HTTP client expose the *same* interface. That is
what lets a handful of tests be written once and run in both places — see the
`any_client` fixture in `tests/conftest.py`.
"""

from __future__ import annotations

import time

import httpx


def live_client(base_url: str, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=timeout)


def probe_service(base_url: str, timeout_s: float) -> str | None:
    """Poll `GET /health` until it answers, or give up.

    Returns `None` when the service is ready, or a human-readable reason when it
    is not — callers turn that into `pytest.skip(reason)` so a missing service
    reads as "skipped: nothing running on :8000", not as a wall of
    `ConnectError` tracebacks.

    This is a *poll with a deadline*, which is the opposite of the banned
    `sleep(5); assume_ready()` pattern: it returns as soon as the condition holds
    and it fails loudly at a bound rather than silently under-waiting on a slow
    runner. The 40 ms simulated-latency middleware in Docker means even a healthy
    service cannot answer instantly, which is exactly why the deadline is in
    seconds and the interval is not tuned to a local-only number.
    """
    deadline = time.monotonic() + timeout_s
    last_error = "no attempt made"
    attempt_interval_s = 0.25

    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("/health")
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return None
                last_error = f"HTTP {response.status_code}: {response.text[:200]!r}"
            time.sleep(attempt_interval_s)

    return (
        f"service at {base_url} was not ready within {timeout_s:g}s "
        f"(last: {last_error}). Start it with `make run-service` or "
        f"`docker compose -f service/docker-compose.yml up -d`, or point BASE_URL elsewhere."
    )
