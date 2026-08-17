"""End-to-end against a running service over real HTTP.

Everything here is chosen to be something the in-process layers *cannot* tell you.
`TestClient` speaks ASGI directly: it never serialises a real HTTP response, never
starts uvicorn, never mounts the static files from disk, and never touches
PostgreSQL. So this layer covers the deployment surface — startup, routing,
static mounts, real JSON on the wire — and deliberately does not re-run the
assertions the fast layers already make.

Isolation here is by unique station ID, because there is no dependency to
override against a live process. Consequently **no test in this file asserts an
absolute network-wide count** — only deltas and station-scoped facts. That
constraint is exactly why the in-process layer exists.

Covered here: an image that serves the API but not the dashboard, a debug
deployment leaking stack traces, the latest-per-station join behaving differently
on PostgreSQL than on SQLite, and `/health` proving liveness rather than
readiness.

Skips cleanly with an actionable message when nothing is listening (see
`live_service` in `tests/conftest.py`) rather than failing with a connection error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.helpers.assertions import assert_status, find_station
from tests.helpers.builders import at, report, station_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

pytestmark = pytest.mark.e2e


@pytest.mark.p0
def test_health_endpoint_answers_over_real_http(live_client: httpx.Client) -> None:
    """The readiness signal CI polls has to be exactly what CI expects.

    Every e2e and perf job waits on this endpoint before running. If its body or
    content type changed, the readiness poll in `tests/helpers/clients.py` would
    hang until timeout and every downstream job would report "service not ready"
    instead of the real failure. Worth pinning precisely for that reason alone.

    Note what it does *not* prove: `/health` returns `{"status": "ok"}`
    unconditionally (`main.py:43-45`) without touching the database, so a green
    health check means "the process is up", not "the service can serve requests"
    That distinction is in the known-issues section, and it is why the
    journey test below exists.
    """
    response = live_client.get("/health")

    assert_status(response, 200)
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.p0
def test_a_station_journey_over_the_wire(live_client: httpx.Client) -> None:
    """The whole operator journey against the real deployment, in deltas.

    Ingest a degrading station, watch it appear in the listing, cross the flagging
    threshold, and show up on the worklist and in the metrics — through a real
    ASGI server and, under Docker, a real PostgreSQL instance rather than SQLite.

    This is the test that catches a defect the fast layers cannot see: a query that
    works on SQLite and not on PostgreSQL. The latest-per-station join
    (`stations.py:15-33`) is exactly the kind of SQL where the two engines differ
    on `GROUP BY` semantics, so running it for real is worth the seconds it costs.
    """
    sid = station_id("E2E")

    before = assert_status(live_client.get("/metrics/summary"), 200)

    healthy = assert_status(
        live_client.post(
            "/reports",
            json=report(station_id_=sid, timestamp=at(), latency_ms=100.0, error_count=0),
        ),
        201,
    )
    assert healthy == {"station_id": sid, "hygiene_score": 95.0, "flagged": False}

    listing = assert_status(live_client.get("/stations"), 200)
    listed = find_station(listing, sid)
    assert listed["flagged"] is False
    assert listed["hygiene_score"] == 95.0

    worklist_ids = [
        s["station_id"] for s in assert_status(live_client.get("/stations/poor-hygiene"), 200)
    ]
    assert sid not in worklist_ids

    # The station degrades: offline, slow, erroring.
    degraded = assert_status(
        live_client.post(
            "/reports",
            json=report(
                station_id_=sid,
                timestamp=at(hours=1),
                connectivity_status="offline",
                latency_ms=600.0,
                error_count=8,
            ),
        ),
        201,
    )
    assert degraded == {"station_id": sid, "hygiene_score": 10.0, "flagged": True}

    status = assert_status(live_client.get(f"/stations/{sid}/status"), 200)
    assert status["connectivity_status"] == "offline"
    assert status["flagged"] is True
    assert status["hygiene_score"] == 10.0

    worklist = assert_status(live_client.get("/stations/poor-hygiene"), 200)
    flagged_entry = find_station(worklist, sid)
    assert flagged_entry["hygiene_score"] == 10.0

    after = assert_status(live_client.get("/metrics/summary"), 200)
    assert after["total_stations"] == before["total_stations"] + 1, (
        "two reports, one new station — deltas, because this database is shared"
    )
    assert after["flagged_count"] == before["flagged_count"] + 1
    assert after["offline_count"] == before["offline_count"] + 1


@pytest.mark.p1
def test_error_responses_serialise_correctly_over_http(live_client: httpx.Client) -> None:
    """422 and 404 survive real serialisation, with real content types.

    In-process tests get a Python object back from the ASGI app; here the body is
    bytes that a real client has to parse. A response-model or exception-handler
    change that breaks JSON encoding — a non-serialisable object in a detail
    field, say — fails here and nowhere else.
    """
    invalid = live_client.post("/reports", json=report(latency_ms=-1.0))
    assert_status(invalid, 422)
    assert invalid.headers["content-type"].startswith("application/json")
    assert invalid.json()["detail"][0]["loc"] == ["body", "latency_ms"]

    missing = live_client.get(f"/stations/{station_id('GHOST')}/status")
    assert_status(missing, 404)
    assert missing.headers["content-type"].startswith("application/json")


@pytest.mark.p2
def test_the_deployment_serves_its_documentation_and_dashboard(
    live_client: httpx.Client,
) -> None:
    """The static mount and the docs routes only exist in a real deployment.

    `main.py:34-40` mounts `/static` and registers `/` **only if the directory
    exists on disk**. That condition is invisible to an in-process test using the
    imported app object from the source tree, and it is precisely what breaks when
    a Dockerfile copies the wrong path — the API works, the dashboard 404s, and
    nobody notices until an operator opens the page.

    `/openapi.json` is asserted too because the contract layer consumes it: if it
    were unreachable in the deployed image, every generated client and every
    schema check downstream would be running against a document that only exists
    on a developer's laptop.
    """
    dashboard = live_client.get("/")
    assert_status(dashboard, 200)
    assert dashboard.headers["content-type"].startswith("text/html")
    assert "NOC Station Health Dashboard" in dashboard.text
    assert 'id="total"' in dashboard.text, "the dashboard's metric tiles are present"

    schema = live_client.get("/openapi.json")
    assert_status(schema, 200)
    assert schema.json()["info"]["title"] == "NOC Station Health API"

    docs = live_client.get("/docs")
    assert_status(docs, 200)
    assert docs.headers["content-type"].startswith("text/html")


@pytest.mark.p2
def test_unknown_paths_do_not_leak_a_stack_trace(live_client: httpx.Client) -> None:
    """A 404 from the real server is a JSON body, not a debug page.

    Cheap deployment check: uvicorn started with `--reload` or a framework debug
    flag renders HTML tracebacks on error, which leaks source paths and local
    variables. The Dockerfile does not enable it (`Dockerfile:11`) and this is what
    keeps it that way.
    """
    response = live_client.get("/no-such-endpoint")

    assert_status(response, 404)
    assert response.headers["content-type"].startswith("application/json")
    assert "Traceback" not in response.text
    assert "/app/" not in response.text, "no filesystem paths in an error response"
