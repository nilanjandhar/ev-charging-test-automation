"""What "latest" means to this service, and where that definition breaks.

Every read endpoint answers "what is the current state of this station?" with
`MAX(timestamp)` over a client-supplied field. That single choice produces one
genuinely good behaviour and two operational hazards, and all three are here.

Risks covered: **R3** (a clock-skewed station pins itself forever), **R6** (UTC
offsets are dropped rather than normalised), plus the out-of-order property that
*does* hold and is worth protecting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.builders import at, future_timestamp, report, station_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.testclient import TestClient

pytestmark = pytest.mark.api


def test_status_reflects_the_newest_timestamp_not_the_newest_arrival(
    api_client: TestClient,
) -> None:
    """R3 (the good half): arrival order must not decide station state.

    Field telemetry arrives out of order routinely — a station buffers reports
    while its uplink is down and flushes them all at once, so a two-hour-old
    report can land after a fresh one. The service ranks by the report's own
    timestamp (`stations.py:52`), so the newest *observation* wins regardless of
    which HTTP request arrived last.

    This is the property that makes the whole ingest path safe to retry, and it
    is one `ORDER BY` away from silently inverting. Note it also makes this test
    genuinely order-independent: the assertion does not depend on POST sequencing.
    """
    sid = station_id()

    api_client.post(
        "/reports",
        json=report(
            station_id_=sid,
            timestamp=at(hours=2),
            connectivity_status="offline",
            latency_ms=500.0,
            error_count=10,
        ),
    )
    # Older observation, newer arrival — must lose.
    api_client.post(
        "/reports",
        json=report(
            station_id_=sid,
            timestamp=at(hours=0),
            connectivity_status="online",
            latency_ms=10.0,
            error_count=0,
        ),
    )

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)

    assert status["latest_timestamp"] == "2024-06-01T12:00:00"
    assert status["connectivity_status"] == "offline"
    assert status["hygiene_score"] == 10.0
    assert status["flagged"] is True

    worklist = assert_status(api_client.get("/stations/poor-hygiene"), 200)
    assert [s["station_id"] for s in worklist] == [sid], (
        "the stale online report must not rescue this station from the worklist"
    )


def test_a_future_dated_report_permanently_masks_every_later_report(
    api_client: TestClient,
) -> None:
    """R3: one bad clock and the station is never heard from again.

    A station with a dead RTC or a failed NTP sync stamps its report in 2099.
    Nothing validates that (`schemas.py:8` accepts any datetime), so from then on
    every genuine report loses the `MAX(timestamp)` comparison. The station's
    status is frozen at whatever it claimed while its clock was wrong.

    The severity depends entirely on *what* it froze at, and the bad case is the
    quiet one: frozen at "online, score 100", the station is permanently green,
    permanently absent from the worklist, and indistinguishable from a healthy
    charger — even after it goes offline and reports 50 errors, as it does here.

    This test pins current behaviour rather than asserting a fix, because the fix
    is a product decision (reject future timestamps? clamp them? rank by
    `created_at`, which the service already stores but never exposes?) and I am
    not entitled to invent one. It is written up as R3 in the known-issues section
    so that whoever makes that decision starts from a reproduction.
    """
    sid = station_id()

    api_client.post(
        "/reports",
        json=report(
            station_id_=sid,
            timestamp=future_timestamp(),
            connectivity_status="online",
            latency_ms=0.0,
            error_count=0,
        ),
    )
    later_real_report = assert_status(
        api_client.post(
            "/reports",
            json=report(
                station_id_=sid,
                timestamp=at(days=1),
                connectivity_status="offline",
                latency_ms=900.0,
                error_count=50,
            ),
        ),
        201,
    )

    # The service scores the real report correctly and then discards it from view.
    assert later_real_report["hygiene_score"] == 10.0
    assert later_real_report["flagged"] is True

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    assert status["latest_timestamp"] == "2099-01-01T00:00:00"
    assert status["connectivity_status"] == "online"
    assert status["hygiene_score"] == 100.0
    assert status["flagged"] is False

    worklist = assert_status(api_client.get("/stations/poor-hygiene"), 200)
    assert worklist == [], "R3: a station that is offline with 50 errors is not on the worklist"

    metrics = assert_status(api_client.get("/metrics/summary"), 200)
    assert metrics["online_count"] == 1, "R3: and the network counts it as online"
    assert metrics["offline_count"] == 0
    assert metrics["flagged_count"] == 0


def test_utc_offsets_are_dropped_rather_than_normalised(api_client: TestClient) -> None:
    """R6: two reports of the same instant are not treated as the same instant.

    `2024-06-01T12:00:00+02:00` and `2024-06-01T10:00:00Z` are the same moment.
    The column is a naive `DateTime` (`models.py:11`), so the offset is discarded
    on the way in and the `+02:00` report is stored as 12:00 — two hours "newer"
    than a UTC report of the same instant, and it wins.

    For a network that crosses a timezone this is a live defect, not a curiosity:
    a Berlin station's stale reading outranks a London station's fresh one, and in
    the other direction a station reporting `-05:00` looks five hours behind and
    can be masked by anything. The suite's own builders always emit UTC, so this
    test is the only place the hazard is visible.
    """
    sid = station_id()

    # Same instant, expressed twice. The UTC one is the unhealthy reading.
    api_client.post(
        "/reports",
        json=report(
            station_id_=sid,
            timestamp="2024-06-01T10:00:00+00:00",
            connectivity_status="offline",
            latency_ms=800.0,
            error_count=9,
        ),
    )
    api_client.post(
        "/reports",
        json=report(
            station_id_=sid,
            timestamp="2024-06-01T12:00:00+02:00",
            connectivity_status="online",
            latency_ms=5.0,
            error_count=0,
        ),
    )

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)

    assert status["latest_timestamp"] == "2024-06-01T12:00:00"
    assert status["connectivity_status"] == "online"
    assert status["flagged"] is False, (
        "R6: the +02:00 report of the *same instant* outranks the UTC one and "
        "clears the flag"
    )


def test_timestamps_lose_their_timezone_on_the_round_trip(api_client: TestClient) -> None:
    """R6: what goes in as UTC comes back with no zone at all.

    A client POSTs `...T10:00:00Z` and reads back `...T10:00:00` — no `Z`, no
    offset. Every consumer must therefore *assume* a zone, and the dashboard does
    exactly that: `new Date(s.latest_timestamp)` at `static/index.html:108` parses
    a naive string as **browser-local** time, so an operator in Los Angeles sees
    every "last report" time shifted by eight hours.

    Small, cheap, and the kind of thing that gets argued about in an incident
    review at 2am, so it is worth a test that states it plainly.
    """
    sid = station_id()

    api_client.post("/reports", json=report(station_id_=sid, timestamp="2024-06-01T10:00:00Z"))

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    listing = assert_status(api_client.get("/stations"), 200)

    assert status["latest_timestamp"] == "2024-06-01T10:00:00"
    assert not status["latest_timestamp"].endswith("Z")
    assert "+" not in status["latest_timestamp"]
    assert listing[0]["latest_timestamp"] == status["latest_timestamp"], (
        "at least the two views agree on the same naive string"
    )


def test_reports_that_tie_on_timestamp_do_not_crash_the_detail_view(
    api_client: TestClient,
) -> None:
    """R1/R11: a tie has to resolve to *something* deterministic per endpoint.

    Two different readings, one timestamp — a station that re-sends with a
    corrected payload but the same clock reading. `/stations/{id}/status` breaks
    the tie with `LIMIT 1`, so it returns exactly one of the two. This test
    asserts it returns *one of the valid serialisations*, not which one: the
    service makes no promise about tie-breaking and SQLite and PostgreSQL will not
    agree. Asserting a specific winner would be a test that passes locally and
    fails in Docker.
    """
    sid = station_id()
    same_time = at()

    api_client.post(
        "/reports",
        json=report(station_id_=sid, timestamp=same_time, latency_ms=100.0, error_count=0),
    )
    api_client.post(
        "/reports",
        json=report(station_id_=sid, timestamp=same_time, latency_ms=300.0, error_count=0),
    )

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)

    assert status["latency_ms"] in (100.0, 300.0)
    assert status["hygiene_score"] in (95.0, 85.0)
    assert status["hygiene_score"] == 100.0 - status["latency_ms"] / 20.0, (
        "whichever row wins, the score must belong to that same row"
    )
