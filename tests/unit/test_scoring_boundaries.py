"""Boundary tests for the hygiene score — the cheapest place to catch the most damage.

What these protect:

* A constant in `service/app/scoring.py` is retuned and every station in the fleet
  re-scores at once. Nothing else in the suite would go red first.
* A fully offline station scores exactly 60.0 and the flag test is `score < 60`,
  so it is *not* flagged — the most operationally severe boundary in the service.
* The error penalty saturates at 6 errors, so a station in a hard error loop is
  scored identically to a mildly unhappy one.

Every expected value in this file is a literal, hand-computed from
`service/README.md:61-69`. That is deliberate: importing the service's own
constants would make these tests tautological — they would pass against any
mutation of the very numbers they exist to protect.
"""

from __future__ import annotations

import pytest
from app.scoring import compute_hygiene_score, is_flagged

pytestmark = pytest.mark.unit


@pytest.mark.p0
@pytest.mark.parametrize(
    ("connectivity", "latency_ms", "error_count", "expected"),
    [
        # (id, connectivity, latency, errors, expected score)
        pytest.param("online", 120.0, 2, 84.0, id="readme-sample-payload"),
        # -- offline penalty: exactly 40 points ------------------------------
        pytest.param("offline", 0.0, 0, 60.0, id="offline-penalty-is-exactly-40"),
        # -- error penalty: 5/error, saturating at 6 errors ------------------
        pytest.param("online", 0.0, 5, 75.0, id="errors-5-just-below-cap"),
        pytest.param("online", 0.0, 6, 70.0, id="errors-6-exactly-at-cap"),
        pytest.param("online", 0.0, 7, 70.0, id="errors-7-just-above-cap"),
        # -- latency penalty: /20, saturating at 400 ms ----------------------
        pytest.param("online", 399.0, 0, 80.05, id="latency-399ms-just-below-cap"),
        pytest.param("online", 400.0, 0, 80.0, id="latency-400ms-exactly-at-cap"),
        pytest.param("online", 401.0, 0, 80.0, id="latency-401ms-just-above-cap"),
        # -- both penalties interacting --------------------------------------
        pytest.param("online", 200.0, 6, 60.0, id="both-capped-region-lands-on-60"),
        pytest.param("online", 201.0, 6, 59.95, id="one-ms-past-60-crosses-threshold"),
        pytest.param("offline", 400.0, 6, 10.0, id="worst-reachable-score-is-10-not-0"),
        # -- rounding: the contract is 2 decimal places ----------------------
        pytest.param("online", 333.33, 0, 83.33, id="rounds-to-two-decimals"),
    ],
)
def test_score_at_each_threshold(
    connectivity: str,
    latency_ms: float,
    error_count: int,
    expected: float,
) -> None:
    """A change to any scoring constant, divisor or cap must go red here.

    At, just below and just above every threshold in the formula, plus the
    interaction of the two capped penalties.

    Why: Hand-computed from the published formula, so a change to any constant fails
        here rather than sliding through both sides.
    """
    assert compute_hygiene_score(connectivity, latency_ms, error_count) == expected


@pytest.mark.p0
@pytest.mark.parametrize(
    ("score", "expected_flagged"),
    [
        pytest.param(59.99, True, id="just-below-threshold-is-flagged"),
        pytest.param(60.0, False, id="exactly-at-threshold-is-NOT-flagged"),
        pytest.param(60.01, False, id="just-above-threshold-is-not-flagged"),
    ],
)
def test_flagging_boundary_is_exclusive(score: float, expected_flagged: bool) -> None:
    """The flag test is strict `<`, so 60.0 exactly is *not* flagged.

    `service/README.md:69` says "falls below 60" and `scoring.py:41` implements
    `score < 60.0`. They agree, so this is not a bug report — it is a lock on an
    inclusive/exclusive decision that is one keystroke away from silently
    inverting which half of the fleet gets dispatched.

    Why: One keystroke separates flagging every dead station from flagging none.
    """
    assert is_flagged(score) is expected_flagged


@pytest.mark.p0
def test_dead_station_reporting_clean_metrics_is_not_flagged() -> None:
    """An offline station with no errors and no latency scores exactly 60.0.

    60.0 is not below 60.0, so this station never appears in
    `/stations/poor-hygiene`. A charger that has stopped talking to the network
    entirely — which is the single thing this service exists to surface — is
    invisible to the operator's worklist.

    Code and README agree (`scoring.py:41`, `README.md:69`), so this is a
    specification defect rather than an implementation bug, and it is pinned here
    rather than xfailed: I am not entitled to invent a threshold. The test exists
    so that fixing it is a *deliberate, reviewed* change with a red test to
    justify, not an accident. Written up in `TEST_STRATEGY.md` under "Known service
    issues".

    Why: The highest-severity finding: pins it so a fix is a deliberate, reviewed change
        rather than an accident.
    """
    score = compute_hygiene_score("offline", latency_ms=0.0, error_count=0)

    assert score == 60.0
    assert is_flagged(score) is False, (
        "this has been fixed or the threshold moved — update the known-issues "
        "section of TEST_STRATEGY.md, this is a behaviour change operators will notice"
    )
