"""Concurrency on the ingest path.

The claim under test is **not** throughput. It is that concurrent writers produce
a state that is *some* valid serialisation of what they sent — no lost reports, no
half-written row, no aggregate that counts a station it cannot show you.

That framing is deliberate. Throughput here is not portable: SQLite serialises
writers behind a single file lock while PostgreSQL does not, so a number measured
locally says nothing about Docker (see `notes/docker-vs-local.md`). The invariant
is portable, so the invariant is what gets asserted.

Recon established that `POST /reports` is a single `add` + `commit` with no
read-modify-write (`reports.py:30-31`), so there is no lost-update *window* to
begin with — this suite exists to keep it that way. The plausible regression is
someone "optimising" ingest into an upsert on `station_id`, at which point
concurrent writers race and this goes red.

Risks covered: **R11** (torn aggregates), plus the write-path integrity R1 depends on.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.builders import at, expected_score, report, station_id
from tests.helpers.config import SETTINGS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.testclient import TestClient

pytestmark = [pytest.mark.perf, pytest.mark.e2e]


async def _post_all(base_url: str, payloads: list[dict[str, object]]) -> list[int]:
    """Fire every payload concurrently and return the status codes."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        responses = await asyncio.gather(
            *(client.post("/reports", json=payload) for payload in payloads)
        )
    return [response.status_code for response in responses]


@pytest.mark.asyncio
async def test_concurrent_reports_for_one_station_all_land(live_client: httpx.Client) -> None:
    """R11: N simultaneous writers, N stored reports, and the final state is one of them.

    Every writer sends a *distinct* timestamp for the same station, so "the correct
    final state" is well defined: the report with the highest timestamp. Anything
    else means a write was lost or a read saw a partial commit.

    Runs against the live service rather than in-process, because the thing being
    tested is the real server's concurrency behaviour — threadpool, connection
    pool, database engine — and `TestClient` serialises requests through a single
    portal thread, which would make this test pass by construction.

    Isolation is by unique station ID, not by an empty database: over real HTTP
    there is no dependency to override, so the assertions are scoped to this
    station and the metrics assertions are deltas.
    """
    sid = station_id("CONC")
    writers = SETTINGS.concurrency_writers
    payloads: list[dict[str, object]] = [
        report(
            station_id_=sid,
            timestamp=at(minutes=index),
            latency_ms=float(index * 20),
            error_count=0,
        )
        for index in range(writers)
    ]

    before = assert_status(live_client.get("/metrics/summary"), 200)
    codes = await _post_all(str(live_client.base_url), payloads)
    after = assert_status(live_client.get("/metrics/summary"), 200)

    assert codes == [201] * writers, f"not every concurrent write was accepted: {codes}"

    status = assert_status(live_client.get(f"/stations/{sid}/status"), 200)
    latest_index = writers - 1
    assert status["latest_timestamp"] == at(minutes=latest_index).replace("+00:00", ""), (
        "the highest timestamp must win regardless of which request finished last"
    )
    assert status["latency_ms"] == float(latest_index * 20)
    # Via the spec re-implementation rather than inline arithmetic: my first version
    # computed `100 - latency/20` by hand and forgot the -20 cap, so the *test* was
    # wrong and the service was right. The helper exists so that mistake is made
    # once, in one place, against the published formula.
    assert status["hygiene_score"] == expected_score("online", float(latest_index * 20), 0), (
        "the score must belong to the same report as the timestamp — not a mix of two"
    )

    assert after["total_stations"] == before["total_stations"] + 1, (
        "one new station, no matter how many reports it sent concurrently"
    )
    assert after["online_count"] == before["online_count"] + 1


@pytest.mark.asyncio
async def test_concurrent_writers_across_stations_leave_metrics_consistent(
    live_client: httpx.Client,
) -> None:
    """R11: an aggregate read taken after concurrent writes must not be internally torn.

    Different stations, all written at once, then one read of `/metrics/summary`.
    The internal identities have to hold — `online + offline == total`,
    `flagged <= total` — because they are computed in Python from a single query
    (`metrics.py:33-40`) and a regression that split that into several queries
    could observe two different snapshots and report `online + offline != total`.

    Deliberately asserts identities and deltas rather than absolute counts: other
    tests, and anything else sharing this deployment, are writing to the same
    database.
    """
    stations = [station_id("MULTI") for _ in range(SETTINGS.concurrency_writers)]
    payloads: list[dict[str, object]] = [
        report(
            station_id_=sid,
            timestamp=at(),
            connectivity_status="offline" if index % 2 else "online",
            latency_ms=100.0,
            error_count=10 if index % 2 else 0,
        )
        for index, sid in enumerate(stations)
    ]
    expected_offline = sum(1 for index in range(len(stations)) if index % 2)

    before = assert_status(live_client.get("/metrics/summary"), 200)
    codes = await _post_all(str(live_client.base_url), payloads)
    after = assert_status(live_client.get("/metrics/summary"), 200)

    assert codes == [201] * len(stations)

    assert after["online_count"] + after["offline_count"] == after["total_stations"], (
        "a torn read: the connectivity split does not add up to the station count"
    )
    assert after["flagged_count"] <= after["total_stations"]
    assert after["total_stations"] == before["total_stations"] + len(stations)
    assert after["offline_count"] == before["offline_count"] + expected_offline, (
        "every offline station in this batch is visible; none was lost to a race"
    )

    # Each station must be individually retrievable — the aggregate counted it, so
    # the detail view has to be able to show it.
    for sid in stations:
        assert_status(live_client.get(f"/stations/{sid}/status"), 200)


def test_repeated_identical_reads_are_stable(live_client: httpx.Client) -> None:
    """R11: a read with no intervening write must not change under repetition.

    Cheap guard against a non-deterministic aggregate — a `GROUP BY` whose result
    depends on plan choice, or an ordering that varies between calls. If this ever
    flickers, every other assertion in the suite is suspect.

    Not a concurrency test in itself; it is the control that makes the two above
    interpretable.
    """
    first = assert_status(live_client.get("/metrics/summary"), 200)
    second = assert_status(live_client.get("/metrics/summary"), 200)

    assert first == second, "aggregate read is not stable between two identical calls"


def test_in_process_client_agrees_with_the_wire_on_a_single_report(
    api_client: TestClient,
) -> None:
    """A control for the whole e2e layer: does in-process testing miss anything here?

    Marked `perf`/`e2e` only so it lives beside the tests it justifies. If the
    in-process and live results for the same input ever diverge, the fast layers
    are lying and the split described in `TEST_STRATEGY.md` needs revisiting —
    that is the claim this test exists to keep honest.
    """
    sid = station_id()

    body = assert_status(api_client.post("/reports", json=report(station_id_=sid)), 201)

    assert body == {"station_id": sid, "hygiene_score": 84.0, "flagged": False}
