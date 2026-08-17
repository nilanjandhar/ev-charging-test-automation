"""Session-wide fixtures.

The one decision worth reading about is **test isolation**, in `api_client` below.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, Verbosity, settings

from tests.helpers import clients
from tests.helpers.config import SETTINGS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    import httpx
    from sqlalchemy.engine import Engine
    from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Import-time side effects of the service under test
# ---------------------------------------------------------------------------
# `service/app/database.py:10` builds the engine at *import* time from
# DATABASE_URL, and `service/app/main.py:10` runs `create_all` against it. So
# merely importing the app writes a `noc.db` into whatever the current working
# directory happens to be. Point that at a scratch directory before anything
# imports the app; the tests themselves never touch this database, because every
# in-process test overrides the dependency (see `api_client`).
_IMPORT_TIME_DB_DIR = tempfile.mkdtemp(prefix="station-health-import-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_IMPORT_TIME_DB_DIR, 'import-time.db')}"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_IMPORT_TIME_DB_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Hypothesis profiles
# ---------------------------------------------------------------------------
# "ci" is derandomised and bounded so a PR gate is reproducible and fast: the same
# commit always explores the same examples, and a red build is re-runnable.
# "nightly" trades that for breadth. Selected with HYPOTHESIS_PROFILE.
settings.register_profile(
    "ci",
    max_examples=50,
    derandomize=True,
    deadline=None,  # a shared CI runner is not a stopwatch; see notes/docker-vs-local.md
    suppress_health_check=[HealthCheck.too_slow],
    print_blob=True,
)
settings.register_profile(
    "nightly",
    max_examples=1000,
    derandomize=False,
    deadline=None,
    verbosity=Verbosity.normal,
    print_blob=True,
)
settings.load_profile(SETTINGS.hypothesis_profile)


# ---------------------------------------------------------------------------
# In-process layer: isolated database per test
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_engine(tmp_path: Any) -> Iterator[Engine]:
    """A private SQLite database, created and destroyed around a single test."""
    from sqlalchemy import create_engine

    from app.database import Base
    from app.models import StationReport  # noqa: F401  (registers the table on Base)

    engine = create_engine(
        f"sqlite:///{tmp_path / 'station-health.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def api_client(isolated_engine: Engine) -> Iterator[TestClient]:
    """In-process ASGI client bound to an empty, private database.

    **Why this and not the obvious alternatives.** The service keeps state in a
    real database (SQLite locally, PostgreSQL in Docker) that survives process
    restarts, so isolation is a data problem, not a process problem. Three
    approaches present themselves, and all three are worse:

    * *Unique station IDs per test.* Needs no reset and is parallel-safe, but
      `GET /metrics/summary` aggregates over every station in the database, so no
      test could ever assert `total_stations == 2`. Aggregates are where the
      interesting bugs are (R1, R5), so giving them up is not a small cost. It is
      still the right answer over real HTTP, where dependency overrides are
      impossible — see `live_client`.
    * *Truncate between tests.* Fast and exact, but it makes the suite
      serial-only: two workers would delete each other's rows mid-test.
    * *Re-import the app per test against a fresh database file.* Correct but
      slow, and it depends on module-reload semantics that break the moment
      anything caches a reference to the app.

    FastAPI already exposes the seam: `get_db` is a dependency
    (`service/app/database.py:15`), so overriding it binds the app to *this*
    test's engine for the duration of *this* test. Empty database, exact
    aggregate assertions, no truncation, parallel-safe, and not one line of
    `service/` is touched. The override is removed afterwards so a leaked
    fixture cannot silently affect the next test.
    """
    from sqlalchemy.orm import Session, sessionmaker
    from starlette.testclient import TestClient

    from app.database import get_db
    from app.main import app

    session_factory = sessionmaker(bind=isolated_engine, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def api_client_observing_500s(isolated_engine: Engine) -> Iterator[TestClient]:
    """Like `api_client`, but reports server errors as a 500 instead of re-raising.

    `TestClient` re-raises unhandled server exceptions by default, which is the
    right default: a test that swallows a stack trace into `assert 500` hides the
    cause. It is wrong for the handful of tests whose *subject* is what a real HTTP
    client sees when the service crashes — over the wire that is a 500 with an
    empty body, and asserting on the Python exception type would be asserting on
    an implementation detail no client can observe.
    """
    from sqlalchemy.orm import Session, sessionmaker
    from starlette.testclient import TestClient

    from app.database import get_db
    from app.main import app

    session_factory = sessionmaker(bind=isolated_engine, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def openapi_schema(api_client: TestClient) -> dict[str, Any]:
    """The service's own published schema — the contract layer's source of truth."""
    response = api_client.get("/openapi.json")
    assert response.status_code == 200, f"cannot fetch /openapi.json: {response.status_code}"
    schema: dict[str, Any] = response.json()
    return schema


# ---------------------------------------------------------------------------
# Live-service layer: e2e / perf / ui
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def live_service() -> str:
    """Skip — clearly — when there is no service to test against.

    Session-scoped so the readiness poll happens once per run, not once per test.
    A missing service must read as `SKIPPED [12] service at http://localhost:8000
    was not ready…`, never as a connection-error traceback: the first is a
    signal about the environment, the second looks like a product defect.
    """
    reason = clients.probe_service(SETTINGS.base_url, SETTINGS.readiness_timeout_s)
    if reason is not None:
        pytest.skip(reason, allow_module_level=True)
    return SETTINGS.base_url


@pytest.fixture
def live_client(live_service: str) -> Iterator[httpx.Client]:
    """Real HTTP against a running service: real ASGI server, real serialisation.

    No dependency override is possible here, so isolation is by unique station ID
    (`tests.helpers.builders.station_id`) and assertions on network-wide
    aggregates are *deltas*, never absolutes.
    """
    with clients.live_client(live_service) as client:
        yield client


@pytest.fixture(params=["in-process", "live-http"])
def any_client(request: pytest.FixtureRequest) -> httpx.Client:
    """Runs a test through both transports where the same assertions hold.

    Used sparingly — only for behaviour that must be identical in-process and over
    the wire (JSON serialisation, status codes, error envelopes). Anything that
    needs an empty database cannot use this, and says so.
    """
    if request.param == "in-process":
        client: httpx.Client = request.getfixturevalue("api_client")
        return client
    request.node.add_marker(pytest.mark.e2e)
    live: httpx.Client = request.getfixturevalue("live_client")
    return live
