"""Schema-driven fuzzing against the service's own OpenAPI document.

`test_openapi_conformance.py` asserts that the responses *I* thought to produce
match the schema. This asserts the same thing for the responses I did not think
of, by generating inputs from the schema itself. The two are complementary: the
handwritten file encodes intent, this one encodes coverage.

Marker-gated (`contract` + `slow`) and bounded by `SCHEMATHESIS_MAX_EXAMPLES` —
15 per operation in the PR gate, 200 nightly. A fuzz suite with an unbounded
example budget is a fuzz suite nobody runs, because it eventually becomes the
slowest thing in the pipeline and gets deleted.

**Which checks are enabled, and why.**

* `not_a_server_error` — a 500 from generated-but-schema-valid input is always a
  defect. This is the check that earns the suite its place.
* `status_code_conformance` — the service must not return codes it does not
  document. One known exception, handled per-operation below.
* `response_schema_conformance` — response bodies match their declared schemas
  across inputs I would not have written by hand.
* `content_type_conformance` and `response_headers_conformance` — cheap, and they
  catch a hand-rolled `Response` that forgets its content type.

**Deliberately not enabled:**

* `negative_data_rejection` — it asserts that schema-invalid input is rejected.
  This service accepts `"120"` for a float because Pydantic's lax mode coerces it,
  which is a deliberate framework default that real embedded clients depend on.
  The behaviour is pinned explicitly in `tests/api/test_ingest_validation.py`
  with the reasoning attached; having a fuzzer re-report it as a failure on every
  run would be noise, and noisy suites get muted.
* `positive_data_acceptance` — the inverse, and it produces the same argument in
  reverse for the same input domain.
* `ignored_auth`, `missing_required_header`, `use_after_free`,
  `ensure_resource_availability`, `unsupported_method` — the service has no
  authentication, no required headers, and no resource lifecycle. These checks
  would pass vacuously, and a vacuous check is worse than an absent one: it
  reports coverage that does not exist.
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

#: The one operation with a known undocumented status code (R9: `stations.py:56`
#: raises 404, the schema declares only 200 and 422). Suppressed for that
#: operation *only*, so an undocumented status code anywhere else still fails.
_R9_OPERATION = "/stations/{station_id}/status"

#: The station the seeding fixture below creates.
SEED_STATION_ID = "FUZZ-SEED-001"

#: Known defects in the service under test, expressed as substrings of the failure
#: schemathesis reports. A run that produces *only* these is tolerated; anything
#: else fails.
#:
#: This allowlist exists instead of switching whole checks off. Excluding
#: `response_schema_conformance` for the three endpoints that return a timestamp
#: would have been one line, and would also have stopped catching every *other*
#: body-shape regression on the three most important endpoints in the service.
#:
#: The allowlist cannot rot silently: both entries have a matching
#: `xfail(strict=True)` test in `test_openapi_conformance.py`, so the moment either
#: defect is fixed those tests XPASS, the build fails, and whoever fixed it is
#: pointed here.
KNOWN_DEFECTS: tuple[tuple[str, str], ...] = (
    (
        "R16",
        # `latest_timestamp` is declared `format: date-time` (RFC 3339, which
        # requires an offset), but the column is a naive DateTime (models.py:11) so
        # the service serialises `2024-06-01T10:00:00` with no zone at all.
        "is not a 'date-time'",
    ),
    (
        "R17",
        # `error_count` is `integer, ge=0` with no maximum, so 2**63 passes
        # validation and overflows the driver. This one is a *server error*, which
        # is normally the last thing to tolerate — it is on the list only because
        # it is pinned by an xfail in tests/api/test_ingest_validation.py and the
        # alternative is a permanently red suite. It is also why this file is
        # `slow` and runs nightly: shrinking that failure costs ~50 seconds.
        "OverflowError",
    ),
)


def _is_known(failure: BaseException) -> str | None:
    """Return the risk ID if this failure is a documented defect, else None.

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
    whether some earlier test had happened to leave rows behind, and R16 was found
    or missed at random.

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


@schema.parametrize()
@settings(max_examples=SETTINGS.schemathesis_max_examples, deadline=None)
def test_operation_conforms_to_its_published_schema(case: Case) -> None:
    """R9/R11/R16: every operation answers schema-valid inputs with schema-valid responses.

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
        # and by the R9 xfail in `test_openapi_conformance.py`.
        case.path_parameters = {"station_id": SEED_STATION_ID}

    try:
        case.call_and_validate(checks=CHECKS, excluded_checks=excluded)
    except FailureGroup as group:
        unrecognised = [failure for failure in group.exceptions if _is_known(failure) is None]
        if unrecognised:
            raise
        # Every failure in this group is a defect already on the record.
        known = sorted({_is_known(failure) or "?" for failure in group.exceptions})
        pytest.xfail(f"known service defect(s) {', '.join(known)} — see TEST_STRATEGY.md")
    except Exception as exc:
        # An unhandled server exception: the ASGI transport re-raises it here
        # instead of turning it into a 500, so it never reaches `not_a_server_error`
        # and never becomes a FailureGroup. R17 arrives by this route.
        risk = _is_known(exc)
        if risk is None:
            raise
        pytest.xfail(f"known service defect {risk} (unhandled {type(exc).__name__}) — see TEST_STRATEGY.md")
