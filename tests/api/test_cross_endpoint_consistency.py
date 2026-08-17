"""Cross-endpoint consistency — the flows an operator actually performs.

Three endpoints each recompute "the latest report per station" with their own SQL
(`stations.py:15`, `stations.py:52`, `stations.py:73`, `metrics.py:14`). Nothing
in the service forces them to agree. These tests assert on *agreement between
endpoints*, not on one endpoint at a time, because the failure that destroys
operator trust is the dashboard saying 42 flagged stations while the worklist
shows 41.

Every test here runs against an empty, private database (see `api_client` in
`tests/conftest.py`), which is what makes exact assertions on network-wide
aggregates possible.

Covered here: the endpoints disagreeing with each other, duplicate reports
double-counting, one absurd report poisoning the network average, and flag
consistency end to end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.helpers.assertions import (
    assert_report_accepted,
    assert_status,
    find_station,
    station_ids,
)
from tests.helpers.builders import at, expected_flagged, expected_score, report, station_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.testclient import TestClient

pytestmark = pytest.mark.api


@pytest.mark.p0
def test_a_single_report_is_reflected_identically_by_every_endpoint(
    api_client: TestClient,
) -> None:
    """Ingest -> status -> list -> metrics must describe the same station.

    The core flow. A report is accepted, and the score it returns has to be the
    score the detail view shows, the score the list view shows, and the numbers
    the dashboard aggregates. The expected score is computed from the published
    formula in `service/README.md`, not imported from `service/app/scoring.py`,
    so a change to the service's constants breaks this test rather than sliding
    through both sides of the comparison.
    """
    sid = station_id()
    payload = report(station_id_=sid, connectivity_status="online", latency_ms=120.0, error_count=2)
    score = expected_score("online", 120.0, 2)

    assert_report_accepted(
        api_client.post("/reports", json=payload),
        station_id=sid,
        score=score,
        flagged=expected_flagged(score),
    )

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    assert status == {
        "station_id": sid,
        "latest_timestamp": "2024-06-01T10:00:00",
        "connectivity_status": "online",
        "latency_ms": 120.0,
        "error_count": 2,
        "firmware_version": "v2.3.1",
        "hygiene_score": score,
        "flagged": False,
    }

    listed = find_station(assert_status(api_client.get("/stations"), 200), sid)
    assert listed["hygiene_score"] == status["hygiene_score"]
    assert listed["flagged"] == status["flagged"]
    assert listed["connectivity_status"] == status["connectivity_status"]
    assert listed["latest_timestamp"] == status["latest_timestamp"]

    metrics = assert_status(api_client.get("/metrics/summary"), 200)
    assert metrics == {
        "total_stations": 1,
        "online_count": 1,
        "offline_count": 0,
        "flagged_count": 0,
        "average_latency_ms": 120.0,
        "total_error_count": 2,
    }


@pytest.mark.p0
def test_flagged_stations_agree_across_list_worklist_and_metrics(
    api_client: TestClient,
) -> None:
    """The poor-hygiene worklist is exactly the set of stations flagged elsewhere.

    An operator triages from `/stations/poor-hygiene` and drills into
    `/stations/{id}/status`. If those two disagree the tool is worse than useless:
    it dispatches technicians to healthy sites and hides dead ones. Asserted as a
    *set* equality in both directions, plus the count the dashboard displays.

    Ordering is deliberately not asserted — `/stations/poor-hygiene` has no
    `ORDER BY` (`stations.py:82-91`) so its order is whatever the engine
    returns and differs between SQLite and PostgreSQL. Pinning today's accident
    would be a test that passes locally and fails in Docker for no useful reason.
    """
    healthy = station_id("OK")
    dead_ish = station_id("BAD")
    borderline = station_id("EDGE")

    api_client.post("/reports", json=report(station_id_=healthy, latency_ms=20.0, error_count=0))
    api_client.post(
        "/reports",
        json=report(
            station_id_=dead_ish, connectivity_status="offline", latency_ms=500.0, error_count=10
        ),
    )
    # Exactly on the threshold: 100 - 30 (6 errors) - 10 (200ms) = 60.0, NOT flagged.
    api_client.post(
        "/reports", json=report(station_id_=borderline, latency_ms=200.0, error_count=6)
    )

    worklist = assert_status(api_client.get("/stations/poor-hygiene"), 200)
    listing = assert_status(api_client.get("/stations"), 200)
    metrics = assert_status(api_client.get("/metrics/summary"), 200)

    flagged_in_worklist = set(station_ids(worklist))
    flagged_in_listing = {s["station_id"] for s in listing if s["flagged"]}

    assert flagged_in_worklist == {dead_ish}
    assert flagged_in_worklist == flagged_in_listing
    assert metrics["flagged_count"] == len(flagged_in_worklist) == 1

    for entry in worklist:
        detail = assert_status(api_client.get(f"/stations/{entry['station_id']}/status"), 200)
        assert detail["flagged"] is True, "a station on the worklist must read as flagged"
        assert detail["hygiene_score"] == entry["hygiene_score"]
        assert detail["latest_timestamp"] == entry["latest_timestamp"]

    # And the borderline station is the flagging boundary, end to end this time:
    edge = assert_status(api_client.get(f"/stations/{borderline}/status"), 200)
    assert edge["hygiene_score"] == 60.0
    assert edge["flagged"] is False
    assert borderline not in flagged_in_worklist


@pytest.mark.p0
def test_metrics_aggregate_only_the_latest_report_per_station(
    api_client: TestClient,
) -> None:
    """Superseded reports must not leak into network totals.

    A station that reported 9 errors an hour ago and 1 error now contributes 1 to
    `total_error_count`, not 10. This is the arithmetic the whole dashboard rests
    on, and it is the assertion that fails if anyone "optimises" the
    latest-per-station join into an aggregate over all history.
    """
    noisy = station_id()
    quiet = station_id()

    api_client.post(
        "/reports",
        json=report(station_id_=noisy, timestamp=at(hours=0), latency_ms=400.0, error_count=9),
    )
    api_client.post(
        "/reports",
        json=report(station_id_=noisy, timestamp=at(hours=1), latency_ms=100.0, error_count=1),
    )
    api_client.post(
        "/reports",
        json=report(
            station_id_=quiet,
            timestamp=at(hours=1),
            connectivity_status="offline",
            latency_ms=200.0,
            error_count=0,
        ),
    )

    metrics = assert_status(api_client.get("/metrics/summary"), 200)

    assert metrics["total_stations"] == 2, "two stations reported, three reports were sent"
    assert metrics["online_count"] == 1
    assert metrics["offline_count"] == 1
    assert metrics["total_error_count"] == 1, "the superseded 9-error report must not count"
    assert metrics["average_latency_ms"] == 150.0, "mean of the two latest: (100 + 200) / 2"
    assert metrics["online_count"] + metrics["offline_count"] == metrics["total_stations"]


@pytest.mark.p1
def test_metrics_on_an_empty_network(api_client: TestClient) -> None:
    """The zero case has to be representable, not a division by zero.

    `average_latency_ms` is `Optional[float]` precisely so this case can return
    null (`metrics.py:38-40`). The dashboard renders it as 'N/A'
    (`static/index.html:94-96`); a 500 here would blank the whole panel on a fresh
    deployment, which is exactly when someone is watching it.
    """
    metrics = assert_status(api_client.get("/metrics/summary"), 200)

    assert metrics == {
        "total_stations": 0,
        "online_count": 0,
        "offline_count": 0,
        "flagged_count": 0,
        "average_latency_ms": None,
        "total_error_count": 0,
    }


@pytest.mark.p2
def test_station_listing_is_ordered_stably(api_client: TestClient) -> None:
    """`/stations` is ordered by station_id and must stay that way.

    Unlike the worklist, this endpoint *does* order (`stations.py:31`). The
    dashboard renders it as a table an operator scans top to bottom, so a
    regression to unordered results would make rows jump between 30-second
    refreshes. Cheap to hold on to; the contrast with `/stations/poor-hygiene`
    above is the point.
    """
    ids = sorted(station_id() for _ in range(5))
    for sid in reversed(ids):  # insert in the opposite order to the one expected
        api_client.post("/reports", json=report(station_id_=sid))

    listing = assert_status(api_client.get("/stations"), 200)

    assert station_ids(listing) == ids


@pytest.mark.p1
def test_one_absurd_latency_report_poisons_the_network_average(
    api_client: TestClient,
) -> None:
    """`latency_ms` has no upper bound and the metric is an unweighted mean.

    A single sensor glitch — or a station sending seconds where the schema expects
    milliseconds — moves the network-wide latency KPI by any amount it likes. The
    station's own score is protected by the -20 cap, so nothing about *that*
    station looks wrong; the damage is entirely in the aggregate an ops lead
    watches.

    Pinned as current behaviour with the domain consequence written down. The fix
    is a sane upper bound in the schema plus a percentile instead of a mean, and
    neither is mine to make.
    """
    normal = station_id()
    glitched = station_id()

    api_client.post("/reports", json=report(station_id_=normal, latency_ms=100.0, error_count=0))
    api_client.post("/reports", json=report(station_id_=glitched, latency_ms=1e12, error_count=0))

    metrics = assert_status(api_client.get("/metrics/summary"), 200)
    glitched_status = assert_status(api_client.get(f"/stations/{glitched}/status"), 200)

    assert metrics["average_latency_ms"] == 500_000_000_050.0
    assert metrics["flagged_count"] == 0, "no station looks unhealthy..."
    assert glitched_status["hygiene_score"] == 80.0, "...because the latency penalty caps at 20"


@pytest.mark.p1
def test_an_infinite_latency_report_erases_the_network_average_entirely(
    api_client: TestClient,
) -> None:
    """One report of `Infinity` and the latency KPI silently becomes null.

    JSON has no infinity literal, but `1e999` parses to `float('inf')` and
    `schemas.py:11` bounds `latency_ms` only from below, so it is accepted. The mean
    becomes inf, and Pydantic serialises a non-finite float as JSON `null`.

    Worse than merely skewing the mean: `null` is what a *healthy, empty* network
    returns, and the dashboard renders both as 'N/A'. The operator cannot tell the
    two apart, and no threshold alert fires on a null.

    Sent as raw content because Python's own `json.dumps` refuses to emit
    non-finite floats — this payload only arrives from a laxer client.
    """
    normal = station_id()
    api_client.post("/reports", json=report(station_id_=normal, latency_ms=100.0, error_count=0))

    assert assert_status(api_client.get("/metrics/summary"), 200)["average_latency_ms"] == 100.0

    infinite = api_client.post(
        "/reports",
        content=(
            '{"station_id":"INF-1","timestamp":"2024-06-01T10:00:00Z",'
            '"connectivity_status":"online","latency_ms":1e999,"error_count":0,'
            '"firmware_version":"v1"}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert_status(infinite, 201)

    metrics = assert_status(api_client.get("/metrics/summary"), 200)

    assert metrics["total_stations"] == 2, "the station is real and counted"
    assert metrics["average_latency_ms"] is None, (
        "indistinguishable from an empty network, and no threshold alert can fire"
    )
    assert metrics["flagged_count"] == 0, "and nothing looks wrong anywhere else"


# ---------------------------------------------------------------------------
# Duplicate reports. Two xfails, both strict.
# ---------------------------------------------------------------------------
# These assert the behaviour I believe is correct, not the behaviour the service
# has. `strict=True` means that if the service is fixed, the XPASS fails the build
# and forces someone to delete the xfail and update the known-issues section —
# which is the only way a known-bug marker stays honest over time.


@pytest.mark.p0
@pytest.mark.xfail(
    strict=True,
    reason=(
        "duplicate (station_id, timestamp) rows both survive the MAX(timestamp) "
        "join at stations.py:26-30, so a retried report lists the station twice"
    ),
)
def test_a_retried_report_does_not_duplicate_the_station_in_the_listing(
    api_client: TestClient,
) -> None:
    """`GET /stations` must list each known station exactly once.

    At-least-once delivery is the norm for field telemetry: an edge gateway
    retries on a timeout, a station re-sends after a TCP reset, a queue replays a
    partition. The service has no uniqueness constraint and no upsert
    (`reports.py:30-31`), so the identical report lands twice and both rows tie on
    `MAX(timestamp)`.

    The brief documents this endpoint as "List all known stations with latest
    status". One entry per known station is the documented contract; two is not.
    """
    sid = station_id()
    payload = report(station_id_=sid)

    assert_status(api_client.post("/reports", json=payload), 201)
    assert_status(api_client.post("/reports", json=payload), 201)

    listing = assert_status(api_client.get("/stations"), 200)

    assert station_ids(listing) == [sid]


@pytest.mark.p0
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the same duplicate rows are counted independently at metrics.py:33-40, "
        "inflating every network-level number"
    ),
)
def test_a_retried_report_does_not_inflate_network_metrics(api_client: TestClient) -> None:
    """One station that reports twice is still one station.

    This is the more expensive half of the defect. `total_stations`, `online_count`,
    `flagged_count` and `total_error_count` are all row counts over the join, so a
    single duplicated report inflates capacity planning, SLA reporting and the
    flagged-station count an operator uses to decide whether tonight is a problem.
    Observed during recon: two real stations, one retry, `total_stations: 3`.
    """
    sid = station_id()
    payload = report(station_id_=sid, error_count=2)

    assert_status(api_client.post("/reports", json=payload), 201)
    assert_status(api_client.post("/reports", json=payload), 201)

    metrics = assert_status(api_client.get("/metrics/summary"), 200)

    assert metrics["total_stations"] == 1
    assert metrics["online_count"] == 1
    assert metrics["total_error_count"] == 2


@pytest.mark.p1
def test_a_retried_report_leaves_the_station_detail_view_correct(
    api_client: TestClient,
) -> None:
    """`/stations/{id}/status` is idempotent.

    Worth its own passing test rather than folding into the xfails above. The
    detail endpoint uses `ORDER BY timestamp DESC LIMIT 1` (`stations.py:52-54`),
    so it collapses the tie and returns one report. That asymmetry is the precise
    shape of the defect: the same duplicate is invisible here and doubled two endpoints
    over, which is how an inconsistency like this survives a casual manual check.
    """
    sid = station_id()
    payload = report(station_id_=sid, latency_ms=120.0, error_count=2)

    first = assert_status(api_client.post("/reports", json=payload), 201)
    second = assert_status(api_client.post("/reports", json=payload), 201)
    assert first == second, "ingest is idempotent in its response, if not in its effect"

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)

    assert status["station_id"] == sid
    assert status["hygiene_score"] == expected_score("online", 120.0, 2)
    assert status["error_count"] == 2, "not 4 — the detail view does not aggregate"
