"""Negative paths on `POST /reports`, and where FastAPI's defaults differ from the brief.

The brief describes a payload; it does not describe what happens to a bad one.
Everything below is FastAPI/Pydantic default behaviour rather than anything the
service author wrote, which is exactly why it is worth pinning: nobody chose it,
so nobody is guarding it, and a future `model_config` change or a major-version
upgrade moves it silently.

Where the defaults are *surprising* relative to the brief, the docstring says so
explicitly — those are the rows that belong in a client-facing API contract and
are missing from it today.

Risks covered: **R12** (unbounded input), plus the general contract risk of an
ingest endpoint that accepts junk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.helpers.assertions import (
    assert_station_absent,
    assert_status,
    assert_validation_error,
)
from tests.helpers.builders import report, station_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.testclient import TestClient

pytestmark = pytest.mark.api


@pytest.mark.parametrize(
    ("overrides", "field", "error_type"),
    [
        pytest.param(
            {"latency_ms": -5.0}, "latency_ms", "greater_than_equal", id="negative-latency"
        ),
        pytest.param(
            {"error_count": -1}, "error_count", "greater_than_equal", id="negative-error-count"
        ),
        pytest.param(
            {"connectivity_status": "degraded"},
            "connectivity_status",
            "literal_error",
            id="unknown-connectivity-value",
        ),
        pytest.param(
            {"connectivity_status": "ONLINE"},
            "connectivity_status",
            "literal_error",
            id="connectivity-is-case-sensitive",
        ),
        pytest.param({"station_id": ""}, "station_id", "string_too_short", id="empty-station-id"),
        pytest.param(
            {"firmware_version": ""}, "firmware_version", "string_too_short", id="empty-firmware"
        ),
        pytest.param({"timestamp": "not-a-date"}, "timestamp", None, id="unparseable-timestamp"),
        pytest.param({"error_count": 2.7}, "error_count", "int_from_float", id="fractional-errors"),
        pytest.param({"station_id": None}, "station_id", "string_type", id="null-station-id"),
        pytest.param(
            {"latency_ms": "fast"}, "latency_ms", "float_parsing", id="non-numeric-latency"
        ),
    ],
)
def test_invalid_field_is_rejected_with_a_machine_readable_error(
    api_client: TestClient,
    overrides: dict[str, Any],
    field: str,
    error_type: str | None,
) -> None:
    """A bad field must produce 422 naming that field — not a 500, not a silent accept.

    The assertion is on `loc` and `type`, never on `msg`. The prose belongs to
    Pydantic and changes between minor versions; a client that branches on the
    error does so on `type`, so that is what the contract is.
    """
    response = api_client.post("/reports", json=report(**overrides))

    assert_validation_error(response, field=field, error_type=error_type)


@pytest.mark.parametrize(
    "missing",
    [
        "station_id",
        "timestamp",
        "connectivity_status",
        "latency_ms",
        "error_count",
        "firmware_version",
    ],
)
def test_every_field_is_required(api_client: TestClient, missing: str) -> None:
    """No field has a server-side default; omitting any one is a 422, not a partial record.

    Worth stating for all six rather than one representative: a `Field(...)` that
    quietly gains a default is how a required field becomes optional without
    anyone noticing, and the resulting rows are silently wrong rather than absent.
    """
    payload = report()
    del payload[missing]

    response = api_client.post("/reports", json=payload)

    assert_validation_error(response, field=missing, error_type="missing")


def test_unknown_fields_are_ignored_and_cannot_override_the_computed_score(
    api_client: TestClient,
) -> None:
    """A client cannot inject its own hygiene score. This is the one that matters.

    Pydantic's default is `extra="ignore"`, so extra keys are dropped rather than
    rejected — which is a lenient contract the brief never mentions, and it cuts
    both ways. The dangerous half would be mass assignment: `hygiene_score` and
    `flagged` are real columns on `StationReport` (`models.py:16-17`), so if the
    ingest model ever grew `extra="allow"` plus a `**payload` construction, a
    station could declare itself healthy. It does not today, and this test is what
    keeps it that way.

    The lenient half is a real if minor cost: a station sending `latency` instead
    of `latency_ms` gets a 422 (missing field), but one sending `error_counts`
    instead of `error_count`... also gets a 422. Typos in *optional-looking*
    positions are the ones that pass silently, and there are none here — so this
    is documented rather than filed as a defect.
    """
    sid = station_id()
    payload = report(
        station_id_=sid,
        latency_ms=120.0,
        error_count=2,
        hygiene_score=100.0,  # attacker-supplied
        flagged=False,  # attacker-supplied
        rogue_field="surprise",
    )

    body = assert_status(api_client.post("/reports", json=payload), 201)

    assert body["hygiene_score"] == 84.0, "the server's computation, not the client's claim"
    assert body["flagged"] is False

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    assert status["hygiene_score"] == 84.0
    assert "rogue_field" not in status, "unknown fields are dropped, not persisted or echoed"


def test_numeric_strings_are_coerced_rather_than_rejected(api_client: TestClient) -> None:
    """Pydantic's lax mode accepts `"120"` for a float. The brief implies it would not.

    Sending JSON strings where the schema says number is a classic symptom of a
    misconfigured gateway or an embedded client with a weak JSON writer. This
    service accepts them and scores them normally.

    I am not calling that a defect — lax coercion is a deliberate Pydantic default
    and rejecting it would break real clients — but it is undocumented behaviour
    that a consumer could reasonably be surprised by, so it is pinned here. If
    someone later sets `strict=True` on the model, this test tells them they have
    made a breaking change for anyone relying on it.
    """
    sid = station_id()
    payload = report(station_id_=sid)
    payload["latency_ms"] = "120"
    payload["error_count"] = "2"

    body = assert_status(api_client.post("/reports", json=payload), 201)

    assert body["hygiene_score"] == 84.0

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    assert status["latency_ms"] == 120.0, "coerced to a float on the way in, not stored as a string"
    assert status["error_count"] == 2


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        pytest.param('{"station_id": ', "application/json", id="truncated-json"),
        pytest.param("hello", "application/json", id="not-json-at-all"),
        pytest.param("[]", "application/json", id="array-instead-of-object"),
        pytest.param("null", "application/json", id="json-null"),
    ],
)
def test_malformed_bodies_are_422_not_500(
    api_client: TestClient, body: str, content_type: str
) -> None:
    """Junk on the wire must never reach the database layer or produce a stack trace.

    Note the status code: FastAPI answers 422 here, where a plain reading of HTTP
    would expect 400 for a syntactically invalid body. That difference matters to
    anyone writing a gateway rule or an alert on 4xx classes, and it is not in the
    brief — so it is asserted rather than assumed.
    """
    response = api_client.post("/reports", content=body, headers={"Content-Type": content_type})

    assert response.status_code == 422, f"got {response.status_code}: {response.text[:200]!r}"
    assert isinstance(response.json()["detail"], list)


def test_unknown_station_is_a_clean_404(api_client: TestClient) -> None:
    """A station nobody has reported for must 404 with a usable message, not an empty 200.

    The distinction is operationally real: "this station has never reported" and
    "this station is fine" must not look the same to a monitoring script. The
    detail string is asserted because it is the only machine-adjacent hint a
    client gets — there is no error code in the body.
    """
    sid = station_id("GHOST")

    assert_station_absent(api_client.get(f"/stations/{sid}/status"), sid)


def test_station_ids_are_matched_exactly(api_client: TestClient) -> None:
    """Lookup is exact-match: no trimming, no case folding, no prefix matching.

    `STATION-1` and `station-1` are different stations to this service. That is a
    defensible choice, but it means a client that upper-cases IDs "for tidiness"
    silently creates a parallel fleet. Pinned so the behaviour is at least written
    down somewhere.
    """
    sid = "STATION-CASE-TEST"
    api_client.post("/reports", json=report(station_id_=sid))

    assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    assert_station_absent(api_client.get(f"/stations/{sid.lower()}/status"), sid.lower())
    assert_station_absent(api_client.get(f"/stations/{sid} /status"), f"{sid} ")


def test_a_large_but_plausible_payload_is_accepted(api_client: TestClient) -> None:
    """R12: neither `station_id` nor `firmware_version` has an upper length bound.

    A 10 KB firmware string is accepted and stored verbatim. `schemas.py:6-12` sets
    `min_length=1` on both string fields and no `max_length`, there is no body-size
    middleware, and uvicorn is started without a body limit
    (`service/Dockerfile:11`).

    This test deliberately uses 10 KB rather than 100 MB. The large-payload test is
    one I decided *not* to automate (see the risk register): it would assert a
    limit the service does not have, so it could only ever pin "unbounded" as
    correct or fail forever. What is worth asserting is the boundary of what is
    plausible — a fat but legitimate firmware identifier round-trips intact — while
    the missing control is documented as belonging at the ingress, not here.
    """
    sid = station_id()
    long_firmware = "v" + "9" * 10_000

    body = assert_status(
        api_client.post("/reports", json=report(station_id_=sid, firmware_version=long_firmware)),
        201,
    )
    assert body["station_id"] == sid

    status = assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    assert status["firmware_version"] == long_firmware, "stored verbatim, no truncation"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "R17: error_count is declared `integer, ge=0` with no maximum, so 2**63 passes "
        "validation and then overflows the database driver — an unhandled 500"
    ),
)
def test_an_enormous_error_count_is_rejected_rather_than_crashing_ingest(
    api_client_observing_500s: TestClient,
) -> None:
    """R17: schema-valid input that reaches the storage layer and blows up there.

    Found by schemathesis, which generated `error_count: 9223372036854775808` —
    exactly 2**63, one past the signed 64-bit maximum. Python integers are
    unbounded so Pydantic accepts it; `models.py:14` is a plain `Integer` column, so
    the driver raises `OverflowError` and FastAPI turns it into a 500.

    Verified boundary: 2**63 - 1 is accepted and scored (69.5); 2**63 is a 500.

    Why it matters beyond tidiness: the ingest endpoint is unauthenticated, so any
    caller — or one station with a corrupted or wrapped counter — turns a 60-byte
    request into a stack trace and a 500. Validation that a storage layer then
    rejects is validation in the wrong place; the fix is a `le=` bound in
    `schemas.py` so the 422 happens at the edge.

    Asserted as the 422 this *should* be, strict, so that the fix retires both this
    marker and the R17 entry in the fuzz suite's known-defect allowlist.
    """
    response = api_client_observing_500s.post("/reports", json=report(error_count=2**63))

    assert_validation_error(response, field="error_count")


def test_the_int64_boundary_below_the_crash_is_accepted(api_client: TestClient) -> None:
    """R17, the other side of the boundary — so the xfail above cannot drift.

    A test that only asserts "2**63 breaks" would keep passing if someone
    accidentally clamped `error_count` to, say, 1000: the crash would be gone and
    the test would look fixed while quietly rejecting legitimate reports. This
    pins the largest value that must keep working.
    """
    sid = station_id()

    body = assert_status(
        api_client.post(
            "/reports", json=report(station_id_=sid, latency_ms=10.0, error_count=2**63 - 1)
        ),
        201,
    )

    assert body["hygiene_score"] == 69.5, "100 - 30 (error cap) - 0.5 (10ms latency)"
    assert body["flagged"] is False


def test_wrong_method_and_missing_route_are_not_500s(api_client: TestClient) -> None:
    """The edges of the routing table: 405 and 404, with JSON bodies.

    Cheap, and it catches the specific failure where a router refactor turns an
    unknown path into an unhandled exception. Also documents that `GET /reports`
    does not exist — there is no way to read back the raw report history, which is
    a genuine gap for anyone auditing why a station was flagged an hour ago.
    """
    assert assert_status(api_client.get("/reports"), 405) == {"detail": "Method Not Allowed"}
    assert assert_status(api_client.get("/stations/nope/nope"), 404) == {"detail": "Not Found"}
    assert assert_status(api_client.delete("/stations"), 405) == {"detail": "Method Not Allowed"}
