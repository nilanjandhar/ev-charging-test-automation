"""Property-based tests for the hygiene score.

`compute_hygiene_score` is the one part of this service that genuinely rewards
Hypothesis: a pure function, a small well-defined input domain, and real algebraic
invariants. Everything else in the service is I/O over a database, where
Hypothesis mostly generates expensive ways to rediscover that SQL works.

Two invariants were tightened rather than taken as offered: the range is asserted
as the *reachable* [10, 100] rather than the documented [0, 100], because the
documented one survives a mutation that doubles every penalty; and monotonicity is
non-increasing rather than strict, because the penalties saturate.

Two more were rejected here entirely — "ingesting the same report twice is
idempotent" and "status always reflects the newest timestamp". Both are stateful
properties about HTTP and a database, and the first is not even universally true
(it holds for `/stations/{id}/status` and is false for `/stations`).
Driving them through Hypothesis would need a function-scoped database fixture
shared across examples — the classic anti-pattern — so they are explicit tests in
`tests/api/` instead.

These guard the scoring constants against regression, and state two blind spots as
invariants rather than examples: the saturation cap, and the fact that an online
station with no errors can never be flagged at any latency.
"""

from __future__ import annotations

import pytest
from app.scoring import FLAGGING_THRESHOLD, compute_hygiene_score, is_flagged
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.unit

# The service's *actual* valid input domain, taken from `service/app/schemas.py:9-12`:
#   latency_ms: float, ge=0        (JSON has no NaN literal and Pydantic rejects NaN
#                                   against ge=0, but +inf survives `1e999` and is
#                                   accepted, so it stays in the domain)
#   error_count: int, ge=0
#   connectivity_status: Literal["online", "offline"]
latency = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
errors = st.integers(min_value=0, max_value=10_000)
connectivity = st.sampled_from(["online", "offline"])


@pytest.mark.p0
@given(connectivity=connectivity, latency_ms=latency, error_count=errors)
def test_score_stays_inside_its_reachable_range(
    connectivity: str, latency_ms: float, error_count: int
) -> None:
    """No input can push the score outside [10, 100].

    The lower bound is 10, not the documented 0: the three penalties sum to at
    most 90. Pinning the tighter bound is what makes this property able to fail —
    a doubled penalty constant escapes 10 long before it escapes 0.

    Why: Asserts the reachable [10, 100], not the documented [0, 100] - the documented
        bound survives a doubled penalty.
    """
    score = compute_hygiene_score(connectivity, latency_ms, error_count)

    assert 10.0 <= score <= 100.0
    assert score == round(score, 2), "the score contract is two decimal places"


@pytest.mark.p0
@given(
    connectivity=connectivity,
    latency_ms=latency,
    error_count=errors,
    extra_errors=st.integers(min_value=0, max_value=10_000),
)
def test_more_errors_never_improves_the_score(
    connectivity: str, latency_ms: float, error_count: int, extra_errors: int
) -> None:
    """Monotonicity in error_count — non-increasing, not strictly decreasing.

    A station that reports *more* problems must never be scored as healthier. The
    weaker "non-increasing" form is the true one because of the -30 cap; asserting
    strict monotonicity would be a false property that fails on the first example
    above six errors.

    Why: A station reporting more problems must never be scored healthier; the universal
        form catches what examples cannot.
    """
    baseline = compute_hygiene_score(connectivity, latency_ms, error_count)
    worse = compute_hygiene_score(connectivity, latency_ms, error_count + extra_errors)

    assert worse <= baseline


@pytest.mark.p0
@given(
    connectivity=connectivity,
    latency_ms=latency,
    error_count=errors,
    extra_latency=st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
)
def test_more_latency_never_improves_the_score(
    connectivity: str, latency_ms: float, error_count: int, extra_latency: float
) -> None:
    """Monotonicity in latency_ms — a slower station is never scored healthier.

    Why: Same guarantee for the other input, and the pair is what makes the score
        defensible to an operator.
    """
    baseline = compute_hygiene_score(connectivity, latency_ms, error_count)
    worse = compute_hygiene_score(connectivity, latency_ms + extra_latency, error_count)

    assert worse <= baseline


@pytest.mark.p1
@given(
    connectivity=connectivity,
    latency_ms=latency,
    error_count=st.integers(min_value=6, max_value=10_000),
    other_error_count=st.integers(min_value=6, max_value=10_000),
)
def test_score_is_blind_to_error_count_above_the_cap(
    connectivity: str, latency_ms: float, error_count: int, other_error_count: int
) -> None:
    """Above 6 errors the score carries no information about error volume.

    This is the saturation blind spot stated as an invariant rather than as a
    single example: from 6 errors upwards, a station reporting a handful of faults
    and a station failing thousands of sessions are indistinguishable to every
    consumer of this API. If someone later makes the penalty unbounded — which is
    the obvious fix — this property goes red and forces the conversation.

    Why: States the saturation blind spot as an invariant, so making the penalty
        unbounded forces the conversation.
    """
    a = compute_hygiene_score(connectivity, latency_ms, error_count)
    b = compute_hygiene_score(connectivity, latency_ms, other_error_count)

    assert a == b


@pytest.mark.p0
@given(latency_ms=latency, error_count=errors)
def test_going_offline_costs_forty_points_give_or_take_the_rounding(
    latency_ms: float, error_count: int
) -> None:
    """The offline penalty is independent of the other two terms — to within 0.01.

    I first wrote this as `online - offline == approx(40.0)` and Hypothesis
    falsified it in under a second. The reason: `scoring.py:37` rounds the
    *final* score, and `round()` is half-even over binary floats, so the two
    scores can round in opposite directions. At `latency_ms=0.1` the difference is
    40.01; at `latency_ms=0.5` it is 39.99.

    The honest property is therefore "40 ± one rounding step". Widening the
    tolerance to something vague like `approx(40, abs=1)` would have hidden the
    finding instead of recording it, and would also stop catching a real
    off-by-one in the constant.

    Why: Catches any refactor that couples the three penalties, e.g. skipping latency
        when offline.
    """
    online = compute_hygiene_score("online", latency_ms, error_count)
    offline = compute_hygiene_score("offline", latency_ms, error_count)

    assert abs((online - offline) - 40.0) <= 0.01 + 1e-9


@pytest.mark.p0
@given(latency_ms=latency)
def test_an_online_station_with_no_errors_can_never_be_flagged(latency_ms: float) -> None:
    """Latency alone is never enough to flag a station, at any value.

    Best-case penalty from latency alone is 20 points, so the floor is 80. A
    charger that is reachable and error-free but takes ten seconds to answer —
    unusable in practice — is scored 80 and stays off the worklist. Stated as a
    property because the interesting part is the *universal* quantifier: there is
    no latency, anywhere in the domain, that rescues this.

    Why: The universal quantifier is the point: no latency anywhere in the domain
        rescues this blind spot.
    """
    score = compute_hygiene_score("online", latency_ms, error_count=0)

    assert score >= 80.0
    assert is_flagged(score) is False


@pytest.mark.p0
@given(connectivity=connectivity, latency_ms=latency, error_count=errors)
def test_flag_agrees_with_the_threshold_for_every_input(
    connectivity: str, latency_ms: float, error_count: int
) -> None:
    """`flagged` is exactly `score < FLAGGING_THRESHOLD`, with no drift.

    The service computes the score and the flag in two separate calls
    (`reports.py:13-18`) and stores both. This is the property that keeps them
    from ever disagreeing — including at the boundary, which Hypothesis reaches
    on its own once it finds the offline/0/0 corner.

    Why: Score and flag are computed and stored separately; this is what stops them
        drifting apart.
    """
    score = compute_hygiene_score(connectivity, latency_ms, error_count)

    assert is_flagged(score) == (score < FLAGGING_THRESHOLD)


@pytest.mark.p1
def test_rounding_step_can_decide_the_flag() -> None:
    """At the boundary the last 0.01 is settled by binary float rounding.

    0.1 ms of latency on an offline station gives a true score of 59.995 — dead on
    the half-step. Python rounds half to even *over the binary representation*, so
    it lands on 59.99 and the station is flagged; the neighbouring value rounds the
    other way. Nothing about which chargers get a truck rolled should be decided
    by IEEE-754 tie-breaking, and this test is here so that statement is on the
    record with a reproducible example attached.

    Why: Puts on record that the last 0.01 of a truck-roll decision is settled by
        IEEE-754 tie-breaking.
    """
    assert compute_hygiene_score("offline", 0.1, 0) == 59.99
    assert compute_hygiene_score("offline", 0.0999, 0) == 60.0
    # ... and the same tie one bracket up rounds the *other* way:
    assert compute_hygiene_score("online", 0.1, 0) == 100.0
