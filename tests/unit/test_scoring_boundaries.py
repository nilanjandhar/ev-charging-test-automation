"""Boundary tests for the hygiene score — the cheapest place to catch the most damage.

Risks covered (see `notes/risk-register.md`):

* **R7** — a constant in `service/app/scoring.py` is retuned and every station in
  the fleet re-scores at once. Nothing else in the suite would go red first.
* **R2** — a fully offline station scores exactly 60.0 and the flag test is
  `score < 60`, so it is *not* flagged. The single most operationally severe
  boundary in the service.
* **R4** — the error penalty saturates at 6 errors, so a station in a hard error
  loop is scored identically to a mildly unhappy one.

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
        pytest.param("online", 0.0, 0, 100.0, id="perfect-station"),
        # -- offline penalty: exactly 40 points ------------------------------
        pytest.param("offline", 0.0, 0, 60.0, id="offline-penalty-is-exactly-40"),
        # -- error penalty: 5/error, saturating at 6 errors ------------------
        pytest.param("online", 0.0, 1, 95.0, id="errors-1-below-cap"),
        pytest.param("online", 0.0, 5, 75.0, id="errors-5-just-below-cap"),
        pytest.param("online", 0.0, 6, 70.0, id="errors-6-exactly-at-cap"),
        pytest.param("online", 0.0, 7, 70.0, id="errors-7-just-above-cap"),
        pytest.param("online", 0.0, 100_000, 70.0, id="errors-100k-far-above-cap-R4"),
        # -- latency penalty: /20, saturating at 400 ms ----------------------
        pytest.param("online", 20.0, 0, 99.0, id="latency-20ms-below-cap"),
        pytest.param("online", 399.0, 0, 80.05, id="latency-399ms-just-below-cap"),
        pytest.param("online", 400.0, 0, 80.0, id="latency-400ms-exactly-at-cap"),
        pytest.param("online", 401.0, 0, 80.0, id="latency-401ms-just-above-cap"),
        pytest.param("online", 1e308, 0, 80.0, id="latency-absurd-still-capped"),
        # -- both penalties interacting --------------------------------------
        pytest.param("online", 200.0, 6, 60.0, id="both-capped-region-lands-on-60"),
        pytest.param("online", 201.0, 6, 59.95, id="one-ms-past-60-crosses-threshold"),
        pytest.param("offline", 400.0, 6, 10.0, id="worst-reachable-score-is-10-not-0"),
        # -- rounding: the contract is 2 decimal places ----------------------
        pytest.param("online", 333.33, 0, 83.33, id="rounds-to-two-decimals"),
        pytest.param("online", 10.0, 0, 99.5, id="half-point-penalty-not-truncated"),
    ],
)
def test_score_at_each_threshold(
    connectivity: str,
    latency_ms: float,
    error_count: int,
    expected: float,
) -> None:
    """R7: a change to any scoring constant, divisor or cap must go red here.

    At, just below and just above every threshold in the formula, plus the
    interaction of the two capped penalties.
    """
    assert compute_hygiene_score(connectivity, latency_ms, error_count) == expected


@pytest.mark.p0
@pytest.mark.parametrize(
    ("score", "expected_flagged"),
    [
        pytest.param(59.99, True, id="just-below-threshold-is-flagged"),
        pytest.param(60.0, False, id="exactly-at-threshold-is-NOT-flagged"),
        pytest.param(60.01, False, id="just-above-threshold-is-not-flagged"),
        pytest.param(0.0, True, id="floor-is-flagged"),
        pytest.param(100.0, False, id="perfect-is-not-flagged"),
    ],
)
def test_flagging_boundary_is_exclusive(score: float, expected_flagged: bool) -> None:
    """R7: the flag test is strict `<`, so 60.0 exactly is *not* flagged.

    `service/README.md:69` says "falls below 60" and `scoring.py:41` implements
    `score < 60.0`. They agree, so this is not a bug report — it is a lock on an
    inclusive/exclusive decision that is one keystroke away from silently
    inverting which half of the fleet gets dispatched.
    """
    assert is_flagged(score) is expected_flagged


@pytest.mark.p0
def test_dead_station_reporting_clean_metrics_is_not_flagged() -> None:
    """R2: an offline station with no errors and no latency scores exactly 60.0.

    60.0 is not below 60.0, so this station never appears in
    `/stations/poor-hygiene`. A charger that has stopped talking to the network
    entirely — which is the single thing this service exists to surface — is
    invisible to the operator's worklist.

    Code and README agree (`scoring.py:41`, `README.md:69`), so this is a
    specification defect rather than an implementation bug, and it is pinned here
    rather than xfailed: I am not entitled to invent a threshold. The test exists
    so that fixing it is a *deliberate, reviewed* change with a red test to
    justify, not an accident. Written up as R2 in `TEST_STRATEGY.md`
    ("Known service issues").
    """
    score = compute_hygiene_score("offline", latency_ms=0.0, error_count=0)

    assert score == 60.0
    assert is_flagged(score) is False, (
        "R2 has been fixed or the threshold moved — update the known-issues "
        "section of TEST_STRATEGY.md, this is a behaviour change operators will notice"
    )


@pytest.mark.p1
def test_error_penalty_saturates_so_catastrophe_is_indistinguishable() -> None:
    """R4: 6 errors and 100,000 errors produce the same score, and neither is flagged.

    A station failing every single charging session reports thousands of errors.
    While it stays reachable it scores 70.0 and never reaches the worklist. The
    cap is deliberate (`ERROR_PENALTY_CAP`), but nothing documents that its
    consequence is a blind spot at exactly the severity an operator most wants to
    see. Pinned so that a future "make the score more sensitive" change has to
    confront it.
    """
    at_cap = compute_hygiene_score("online", latency_ms=0.0, error_count=6)
    catastrophic = compute_hygiene_score("online", latency_ms=0.0, error_count=100_000)

    assert at_cap == catastrophic == 70.0
    assert is_flagged(catastrophic) is False


@pytest.mark.p2
def test_score_floor_is_ten_making_the_zero_clamp_unreachable() -> None:
    """R7 / dead code: the documented range is [0, 100] but the reachable range is [10, 100].

    Maximum total penalty is 40 (offline) + 30 (error cap) + 20 (latency cap) = 90,
    so `max(score, 0.0)` at `scoring.py:37` can never fire. Worth pinning for two
    reasons: it is the branch a coverage report will show as "covered" while being
    logically dead, and if someone raises a penalty cap this test tells them the
    clamp has just become live — which changes the meaning of a score of 0 from
    "impossible" to "at least this bad".
    """
    worst = compute_hygiene_score("offline", latency_ms=1e9, error_count=1_000_000)

    assert worst == 10.0
    assert worst > 0.0
