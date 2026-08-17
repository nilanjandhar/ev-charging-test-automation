"""Schema-driven fuzzing against the service's own OpenAPI document.

`test_openapi_conformance.py` covers the responses I thought of; this covers the
ones I did not, by generating inputs from the schema. Marker-gated (`contract` +
`slow`) and bounded by `SCHEMATHESIS_MAX_EXAMPLES` — 15 per operation in CI, 200
nightly — because an unbounded fuzz suite eventually becomes the slowest thing in
the pipeline and gets deleted.

Checks enabled: `not_a_server_error` (a 500 from schema-valid input is always a
defect — this is what earns the suite its place), plus status-code, schema,
content-type and header conformance. Not enabled: `negative_data_rejection` and
`positive_data_acceptance`, because this service deliberately accepts `"120"` for
a float (Pydantic lax mode, pinned in `test_ingest_validation.py`) and a fuzzer
re-reporting that every night is noise; and the auth/header/lifecycle checks,
which would pass vacuously against a service that has none of those things.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

import pytest
import schemathesis
from hypothesis import settings
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from tests.helpers.config import SETTINGS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from schemathesis import Case

pytestmark = [
    pytest.mark.contract,
    pytest.mark.slow,
    # schemathesis' ASGI transport leaves anyio memory streams to the collector;
    # with `filterwarnings = error` the resulting ResourceWarning surfaces as an
    # unraisable-exception error against whichever test happens to trigger GC.
    # Not ours to fix and not a signal about the service.
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
]

# Fuzzing writes real rows, so give it its own database rather than the one the
# import-time engine points at. It never asserts on aggregates, so it does not
# need per-example isolation — only somewhere harmless to write.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='schemathesis-')}/fuzz.db"

from app.main import app  # noqa: E402  (must follow the DATABASE_URL assignment)

schema = schemathesis.openapi.from_asgi("/openapi.json", app)

CHECKS = [
    schemathesis.checks.not_a_server_error,
    status_code_conformance,
    response_schema_conformance,
    content_type_conformance,
    response_headers_conformance,
]

#: The one operation with a known undocumented status code: `stations.py:56` raises
#: 404 while the schema declares only 200 and 422. Suppressed for that operation
#: *only*, so an undocumented status code anywhere else still fails.
_R9_OPERATION = "/stations/{station_id}/status"

#: The station the seeding fixture below creates.
SEED_STATION_ID = "FUZZ-SEED-001"

#: Known defects in the service under test, each as a name and a substring of the
#: failure schemathesis reports. A run that produces *only* these is tolerated;
#: anything else fails.
#:
#: This allowlist exists instead of switching whole checks off. Excluding
#: `response_schema_conformance` for the three endpoints that return a timestamp
#: would have been one line, and would also have stopped catching every *other*
#: body-shape regression on the three most important endpoints in the service.
#:
#: The allowlist cannot rot silently: both entries have a matching
#: `xfail(strict=True)` test elsewhere in the suite, so the moment either defect is
#: fixed those tests XPASS, the build fails, and whoever fixed it is pointed here.
KNOWN_DEFECTS: tuple[tuple[str, str], ...] = (
    (
        "naive-datetime",
        # `latest_timestamp` is declared `format: date-time` (RFC 3339, which
        # requires an offset), but the column is a naive DateTime (models.py:11) so
        # the service serialises `2024-06-01T10:00:00` with no zone at all.
        "is not a 'date-time'",
    ),
    (
        "unbounded-error-count",
        # `error_count` is `integer, ge=0` with no maximum, so 2**63 passes
        # validation and overflows the driver. A *server error* is normally the last
        # thing to tolerate; it is here only because it is pinned by an xfail in
        # tests/api/test_ingest_validation.py and the alternative is a permanently
        # red suite. It is also why this file is `slow` and runs nightly: shrinking
        # that failure costs ~50 seconds.
        "OverflowError",
    ),
)


def _is_known(failure: BaseException) -> str | None:
    """Return the defect's name if this failure is a documented one, else None.

    Matches the exception type name as well as its message: an unhandled server
    error arrives here as the original exception (the ASGI transport re-raises it
    rather than synthesising a 500), and `OverflowError`'s message is the driver's
    prose, not the type.
    """
    text = f"{type(failure).__name__}: {failure}"
    return next((risk for risk, marker in KNOWN_DEFECTS if marker in text), None)


@pytest.fixture(scope="module", autouse=True)
def _seed_one_station() -> None:
    """Make sure the read endpoints have something to serialise.

    Found while running the suite three times in a row and getting three different
    xfail counts. Against an empty database the collection endpoints return `[]`,
    which validates against any item schema — so the fuzzer's verdict on
    `/stations`, `/stations/poor-hygiene` and `/stations/{id}/status` depended on
    whether some earlier test had happened to leave rows behind, so the naive-datetime
    defect was found or missed at random.

    A vacuously-passing fuzz test is the worst possible outcome: it reports
    coverage it does not have. One seeded report makes the payloads non-empty and
    the result deterministic. It is also the only shared state in the suite, and it
    is safe precisely because nothing here asserts on aggregates — see
    `TEST_STRATEGY.md` on why the fuzz layer gets its own database.
    """
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        client.post(
            "/reports",
            json={
                "station_id": SEED_STATION_ID,
                "timestamp": "2024-06-01T10:00:00Z",
                "connectivity_status": "offline",
                "latency_ms": 500.0,
                "error_count": 10,
                "firmware_version": "v2.3.1",
            },
        )


@pytest.mark.p1
@schema.parametrize()
@settings(max_examples=SETTINGS.schemathesis_max_examples, deadline=None)
def test_operation_conforms_to_its_published_schema(case: Case) -> None:
    """Every operation answers schema-valid inputs with schema-valid responses.

    One test per documented operation, so a failure names the endpoint rather than
    "the fuzz suite". Failures that are already-documented service defects are
    tolerated by risk ID; anything unrecognised fails the build.
    """
    from schemathesis.core.failures import FailureGroup

    excluded = [status_code_conformance] if case.path == _R9_OPERATION else []

    if case.path == _R9_OPERATION:
        # Point the path parameter at a station that exists. A randomly generated
        # ID 404s, and a 404 body has no timestamp to validate — so with random IDs
        # this operation's verdict flipped between runs depending on whether the
        # fuzzer happened to guess a real station. Pinning the ID exercises the 200
        # response every time; the 404 path is covered deterministically by
        # `tests/api/test_ingest_validation.py::test_unknown_station_is_a_clean_404`
        # and by the undocumented-404 xfail in `test_openapi_conformance.py`.
        case.path_parameters = {"station_id": SEED_STATION_ID}

    try:
        case.call_and_validate(checks=CHECKS, excluded_checks=excluded)
    except FailureGroup as group:
        unrecognised = [failure for failure in group.exceptions if _is_known(failure) is None]
        if unrecognised:
            raise
        # Every failure in this group is a defect already on the record.
        known = sorted({_is_known(failure) or "?" for failure in group.exceptions})
        pytest.xfail(f"known service defect(s): {', '.join(known)} — see TEST_STRATEGY.md")
    except Exception as exc:
        # An unhandled server exception: the ASGI transport re-raises it here
        # instead of turning it into a 500, so it never reaches `not_a_server_error`
        # and never becomes a FailureGroup. The overflow arrives by this route.
        risk = _is_known(exc)
        if risk is None:
            raise
        pytest.xfail(
            f"known service defect {risk} (unhandled {type(exc).__name__}) — see TEST_STRATEGY.md"
        )
