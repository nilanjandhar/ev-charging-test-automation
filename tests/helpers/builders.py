"""Test-data builder for health reports.

One builder, not a fixture per scenario. Scenario fixtures rot: each new edge
case adds another near-duplicate, and a reader has to jump to conftest to learn
what a test is actually sending. A builder keeps the interesting field visible at
the call site::

    report(connectivity_status="offline", latency_ms=0, error_count=0)

Two rules the whole suite depends on:

* **Timestamps are fixed, never `now()`.** A test that reads the wall clock is a
  test that behaves differently at 23:59 UTC, and this service ranks reports by
  client-supplied timestamp, so `now()` would make recency assertions racy.
* **Station IDs are unique and opaque.** They exist so that a test's rows cannot
  collide with another test's rows in a shared database (the e2e layer, where
  dependency overrides are impossible). No assertion ever depends on the *shape*
  of an ID — only on identity.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

ConnectivityStatus = Literal["online", "offline"]

#: Every timestamp in the suite is an offset from this instant. Chosen to match
#: the sample payload in the service README so hand-checking against the docs is
#: trivial. Deliberately in the past: see `future_timestamp` for the clock-skew case.
BASE_TIME = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

DEFAULT_FIRMWARE = "v2.3.1"


def station_id(prefix: str = "ST") -> str:
    """A station ID unique across processes, runs and parallel workers."""
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def at(**delta: float) -> str:
    """An ISO-8601 UTC timestamp offset from :data:`BASE_TIME`.

    ``at(minutes=5)`` -> ``'2024-06-01T10:05:00+00:00'``.
    """
    return (BASE_TIME + timedelta(**delta)).isoformat()


def future_timestamp() -> str:
    """A timestamp far enough ahead that no real report can ever outrank it (risk R3)."""
    return "2099-01-01T00:00:00+00:00"


def report(
    *,
    station_id_: str | None = None,
    timestamp: str | None = None,
    connectivity_status: ConnectivityStatus = "online",
    latency_ms: float = 120.0,
    error_count: int = 2,
    firmware_version: str = DEFAULT_FIRMWARE,
    **extra: Any,
) -> dict[str, Any]:
    """A valid `POST /reports` payload; override any field, add any extra field.

    Defaults are the sample payload from `service/README.md` (score 84.0, not
    flagged) so that "the documented happy path" needs no arguments.

    `extra` exists for negative tests that need fields the schema does not define.
    """
    payload: dict[str, Any] = {
        "station_id": station_id_ if station_id_ is not None else station_id(),
        "timestamp": timestamp if timestamp is not None else at(),
        "connectivity_status": connectivity_status,
        "latency_ms": latency_ms,
        "error_count": error_count,
        "firmware_version": firmware_version,
    }
    payload.update(extra)
    return payload


def expected_score(
    connectivity_status: str,
    latency_ms: float,
    error_count: int,
) -> float:
    """The hygiene score, computed from the *specification* rather than the code.

    This is a deliberate duplicate of `service/app/scoring.py`, written from
    `service/README.md:61-69`. The point is that it is an independent second
    implementation: if someone edits the service's constants, the two disagree
    and the boundary tests go red. Importing the service's own constants instead
    would make those tests tautological — they would pass against any mutation.
    """
    score = 100.0
    if connectivity_status == "offline":
        score -= 40.0
    score -= min(error_count * 5.0, 30.0)
    score -= min(latency_ms / 20.0, 20.0)
    return round(max(score, 0.0), 2)


def expected_flagged(score: float) -> bool:
    """Flagged when the score falls *below* 60 — strictly, per `service/README.md:69`."""
    return score < 60.0
