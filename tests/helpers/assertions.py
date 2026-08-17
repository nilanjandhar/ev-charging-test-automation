"""Shared assertions.

These exist to enforce one house rule: no test asserts on a status code alone.
Every helper checks the code *and* the body's shape and values, and every failure
message says what was actually received, because a red test that only says
`assert 422 == 201` costs someone twenty minutes.
"""

from __future__ import annotations

from typing import Any

import httpx


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:  # pragma: no cover - only on a non-JSON failure response
        return response.text


def assert_status(response: httpx.Response, expected: int) -> Any:
    """Assert the status code and return the decoded body for further assertions."""
    assert response.status_code == expected, (
        f"{response.request.method} {response.request.url.path} "
        f"-> {response.status_code}, expected {expected}. Body: {_body(response)!r}"
    )
    return _body(response)


def assert_report_accepted(
    response: httpx.Response,
    *,
    station_id: str,
    score: float,
    flagged: bool,
) -> None:
    """`POST /reports` returned 201 with the exact computed score and flag."""
    body = assert_status(response, 201)
    assert body == {
        "station_id": station_id,
        "hygiene_score": score,
        "flagged": flagged,
    }, f"unexpected ingest response body: {body!r}"


def assert_validation_error(
    response: httpx.Response,
    *,
    field: str,
    error_type: str | None = None,
) -> None:
    """A 422 whose machine-readable detail points at `field`.

    Asserts on `loc` and `type`, never on `msg`: the prose belongs to Pydantic and
    changes on minor upgrades, so pinning it buys upgrade toil and no signal.
    """
    body = assert_status(response, 422)
    assert isinstance(body, dict), f"expected a JSON object, got {body!r}"
    assert isinstance(body.get("detail"), list), (
        f"expected FastAPI's validation envelope with a list `detail`, got {body!r}"
    )
    locations = [tuple(item.get("loc", ())) for item in body["detail"]]
    assert any(field in loc for loc in locations), (
        f"no validation error mentions {field!r}; got locations {locations!r}"
    )
    if error_type is not None:
        types = [item.get("type") for item in body["detail"]]
        assert error_type in types, f"expected error type {error_type!r}, got {types!r}"


def assert_station_absent(response: httpx.Response, station_id: str) -> None:
    """`GET /stations/{id}/status` 404s with the documented detail message."""
    body = assert_status(response, 404)
    assert body == {"detail": f"Station '{station_id}' not found"}, f"unexpected 404 body: {body!r}"


def station_ids(stations: list[dict[str, Any]]) -> list[str]:
    return [s["station_id"] for s in stations]


def find_station(stations: list[dict[str, Any]], station_id: str) -> dict[str, Any]:
    """The single entry for `station_id`; fails loudly if it is missing or duplicated.

    The duplicate check is not incidental: a retried report makes this list contain
    the same station twice.
    """
    matches = [s for s in stations if s["station_id"] == station_id]
    assert matches, f"station {station_id!r} not in {station_ids(stations)!r}"
    assert len(matches) == 1, (
        f"station {station_id!r} appears {len(matches)} times in the response "
        f"(duplicate rows survive the latest-per-station join): {matches!r}"
    )
    return matches[0]
