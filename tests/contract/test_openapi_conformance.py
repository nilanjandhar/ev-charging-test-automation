"""Do real responses match the schema the service publishes about itself?

The service generates `/openapi.json` from its own Pydantic models, so it can
never contradict itself on *field types* — that part is free. What it can and does
get wrong is everything the models do not cover: which status codes an endpoint
can return, and whether the documented set is complete.

That asymmetry decides what these tests do. Validating a 200 body against its
declared schema is cheap insurance against someone hand-writing a `response_model`
or adding a `JSONResponse`; validating the *error* responses is where the actual
finding is.

OpenAPI 3.1 schemas are JSON Schema 2020-12, so `jsonschema` validates them
directly with no translation layer — see `TEST_STRATEGY.md` for why that beat
pulling in a heavier OpenAPI-specific validator.

Covered here: the undocumented 404, and response-shape drift between endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from tests.helpers.assertions import assert_status
from tests.helpers.builders import report, station_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx
    from starlette.testclient import TestClient

pytestmark = pytest.mark.contract


#: Base URI the whole OpenAPI document is registered under, so that the
#: `#/components/schemas/...` references inside it resolve against the document
#: rather than against the fragment being validated.
_DOCUMENT_URI = "urn:station-health-openapi"


def _pointer(*tokens: str) -> str:
    """RFC 6901 JSON pointer, escaping `~` and `/` inside each token.

    Needed because the path templates are themselves segments —
    `/stations/{station_id}/status` has to survive as a single token.
    """
    escaped = (token.replace("~", "~0").replace("/", "~1") for token in tokens)
    return "/".join(("", *escaped))


def _validator_for(
    openapi: dict[str, Any], path: str, method: str, status: str
) -> Draft202012Validator:
    """Build a validator for one documented (path, method, status) response body."""
    try:
        response_spec = openapi["paths"][path][method.lower()]["responses"][status]
        response_spec["content"]["application/json"]["schema"]
    except KeyError as exc:  # pragma: no cover - a missing entry is the assertion's job
        pytest.fail(f"{method} {path} does not document a JSON {status} response (missing {exc})")

    registry = Registry().with_resource(_DOCUMENT_URI, DRAFT202012.create_resource(openapi))
    ref = (
        _DOCUMENT_URI
        + "#"
        + _pointer(
            "paths",
            path,
            method.lower(),
            "responses",
            status,
            "content",
            "application/json",
            "schema",
        )
    )
    return Draft202012Validator({"$ref": ref}, registry=registry)


def assert_conforms(
    openapi: dict[str, Any], response: httpx.Response, path: str, method: str = "GET"
) -> Any:
    """Assert the response body validates against its own documented schema."""
    status = str(response.status_code)
    validator = _validator_for(openapi, path, method, status)
    body = response.json()

    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(
        f"{method} {path} -> {status}: "
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors
    )
    return body


@pytest.mark.p1
def test_the_service_publishes_a_valid_openapi_document(openapi_schema: dict[str, Any]) -> None:
    """The schema itself must be well-formed 3.1 and cover every documented endpoint.

    This is the test that fails if someone removes a router, renames a path, or
    downgrades the FastAPI major version underneath us. The endpoint list is
    asserted explicitly rather than counted: a count of 6 passes when a path is
    renamed, and renaming a path is a breaking change for every client.

    Why: The contract layer and any generated client both consume this document; a
        renamed path breaks every consumer.
    """
    assert openapi_schema["openapi"].startswith("3.1"), (
        f"expected OpenAPI 3.1 (JSON Schema 2020-12); got {openapi_schema['openapi']}"
    )
    assert openapi_schema["info"] == {
        "title": "NOC Station Health API",
        "description": (
            "Ingests EV charging station health reports and computes network hygiene scores."
        ),
        "version": "1.0.0",
    }

    documented = {
        (method.upper(), path)
        for path, operations in openapi_schema["paths"].items()
        for method in operations
    }
    assert documented == {
        ("POST", "/reports"),
        ("GET", "/stations"),
        ("GET", "/stations/{station_id}/status"),
        ("GET", "/stations/poor-hygiene"),
        ("GET", "/metrics/summary"),
        ("GET", "/health"),
    }


@pytest.mark.p1
def test_every_documented_success_response_matches_its_schema(
    api_client: TestClient, openapi_schema: dict[str, Any]
) -> None:
    """One flow, every endpoint, each response validated against its own contract.

    Driven through a single ingest so the responses describe a real station rather
    than empty collections — an empty list validates against almost any item
    schema, which would make this test look green while asserting nothing.

    Why: Catches a hand-written `response_model` or a raw `JSONResponse` drifting from
        the published shape.
    """
    sid = station_id()
    ingest = api_client.post(
        "/reports",
        json=report(
            station_id_=sid, connectivity_status="offline", latency_ms=500.0, error_count=10
        ),
    )

    assert_conforms(openapi_schema, ingest, "/reports", "POST")
    assert ingest.status_code == 201, "the documented success code for ingest is 201, not 200"

    listing = assert_conforms(openapi_schema, api_client.get("/stations"), "/stations")
    assert len(listing) == 1, "a non-empty list, so the item schema is actually exercised"

    status = assert_conforms(
        openapi_schema,
        api_client.get(f"/stations/{sid}/status"),
        "/stations/{station_id}/status",
    )
    assert status["station_id"] == sid

    worklist = assert_conforms(
        openapi_schema, api_client.get("/stations/poor-hygiene"), "/stations/poor-hygiene"
    )
    assert len(worklist) == 1

    metrics = assert_conforms(
        openapi_schema, api_client.get("/metrics/summary"), "/metrics/summary"
    )
    assert metrics["total_stations"] == 1

    assert_conforms(openapi_schema, api_client.get("/health"), "/health")


@pytest.mark.p1
def test_validation_errors_match_the_documented_error_schema(
    api_client: TestClient, openapi_schema: dict[str, Any]
) -> None:
    """The 422 envelope is part of the contract and clients parse it.

    `HTTPValidationError` is a published component. Anything that changes its
    shape — a custom exception handler, a FastAPI major bump — breaks every client
    that reads `detail[].loc` to highlight a bad field, and would go unnoticed
    without this.

    Why: Clients read `detail[].loc` to highlight a bad field; a custom exception
        handler would break them silently.
    """
    response = api_client.post("/reports", json=report(latency_ms=-1.0))

    body = assert_conforms(openapi_schema, response, "/reports", "POST")
    assert body["detail"][0]["loc"] == ["body", "latency_ms"]


@pytest.mark.p2
@pytest.mark.xfail(
    strict=True,
    reason=(
        "stations.py:56 raises 404 for an unknown station, but the OpenAPI "
        "document for that path declares only 200 and 422"
    ),
)
def test_the_404_the_service_actually_returns_is_documented(
    openapi_schema: dict[str, Any],
) -> None:
    """A status code the service returns must appear in the schema it publishes.

    The one contract failure a code-generated schema cannot catch by construction:
    `HTTPException(404)` is raised in the handler body, so FastAPI cannot infer it
    and nobody added `responses={404: ...}`. A client generated from this schema
    therefore has no branch for 404 — which is the entire reason a service
    publishes one.

    Why: The one contract failure a code-generated schema cannot catch by construction.
    """
    responses = openapi_schema["paths"]["/stations/{station_id}/status"]["get"]["responses"]

    assert "404" in responses, (
        f"documented statuses are {sorted(responses)}; the service also returns 404"
    )


@pytest.mark.p1
@pytest.mark.xfail(
    strict=True,
    reason=(
        "latest_timestamp is declared `format: date-time` (RFC 3339 requires a "
        "UTC offset) but models.py:11 stores a naive DateTime, so the service "
        "serialises '2024-06-01T10:00:00' with no zone"
    ),
)
def test_timestamps_conform_to_the_date_time_format_they_declare(
    api_client: TestClient, openapi_schema: dict[str, Any]
) -> None:
    """The service violates its own published `format: date-time`.

    Found by schemathesis, and the reason is worth recording: `format` is an
    *annotation* in JSON Schema, so `jsonschema` ignores it unless given a format
    checker — which is why the structural tests above pass. Two tools, two default
    strictness levels, and the gap between them was a real bug.

    RFC 3339 requires an offset, so `2024-06-01T10:00:00` is not a `date-time`. A
    strict generated client rejects it; a lenient one parses it as browser-local
    time, which is what the dashboard does (`static/index.html:108`).

    Why: Pins a live defect the structural checks miss, because JSON Schema treats
        `format` as an annotation.
    """
    from jsonschema import FormatChecker

    sid = station_id()
    api_client.post("/reports", json=report(station_id_=sid))

    schema = openapi_schema["components"]["schemas"]["StationStatus"]["properties"]
    assert schema["latest_timestamp"] == {
        "type": "string",
        "format": "date-time",
        "title": "Latest Timestamp",
    }, "the premise of this test is that the field declares RFC 3339"

    body = assert_status(api_client.get(f"/stations/{sid}/status"), 200)
    validator = Draft202012Validator(
        {"type": "string", "format": "date-time"}, format_checker=FormatChecker()
    )
    errors = [error.message for error in validator.iter_errors(body["latest_timestamp"])]

    assert not errors, f"{body['latest_timestamp']!r}: {errors}"
