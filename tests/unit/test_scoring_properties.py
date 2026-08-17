"""Property-based tests for the hygiene score.

`compute_hygiene_score` is the one part of this service that genuinely rewards
Hypothesis: a pure function, a small well-defined input domain, and real algebraic
invariants. Everything else in the service is I/O over a database, where
Hypothesis mostly generates expensive ways to rediscover that SQL works.

**Which of the suggested invariants I kept, and which I rejected.**

* *"score is always within its documented range"* — kept, but tightened. The
  documented range is [0, 100]; the **reachable** range is [10, 100] (max penalty
  is 90). Asserting the documented range would pass against a mutation that
  doubled every penalty. Asserting the reachable one would not.
* *"increasing error_count never increases the score"* — kept as stated
  (non-increasing). It is tempting to write it as strictly decreasing; that is
  false, because the penalty saturates at 6 errors. The saturation deserves its
  own property, so it gets one.
* *"increasing latency_ms never increases the score"* — same, saturating at 400 ms.
* *"ingesting the same report twice yields the same station status as once"* —
  **rejected here, and it is not universally true.** It holds for
  `/stations/{id}/status` and is false for `/stations` and `/metrics/summary`
  (risk R1: both tied rows survive the latest-per-station join). It is a stateful
  property about HTTP and a database, so it lives in
  `tests/api/test_cross_endpoint_consistency.py` where it can be stated precisely
  per endpoint. Driving it through Hypothesis would mean a function-scoped
  database fixture shared across examples — the classic Hypothesis anti-pattern.
* *"a station's status always reflects its most recent report by timestamp,
  regardless of arrival order"* — same reasoning; it is an API-layer property and
  it is covered explicitly in `tests/api/test_recency_semantics.py`.

Risks covered: **R7** (constant regressions), **R4** (saturation blind spot),
**R2** (an online station with no errors can never be flagged).
"""

from __future__ import annotations

import pytest
from app.scoring import FLAGGING_THRESHOLD, compute_hygiene_score, is_flagged
from hypothesis import assume, given
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
    """R7: no input can push the score outside [10, 100].

    The lower bound is 10, not the documented 0: the three penalties sum to at
    most 90. Pinning the tighter bound is what makes this property able to fail —
    a doubled penalty constant escapes 10 long before it escapes 0.
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
    """R7: monotonicity in error_count — non-increasing, not strictly decreasing.

    A station that reports *more* problems must never be scored as healthier. The
    weaker "non-increasing" form is the true one because of the -30 cap; asserting
    strict monotonicity would be a false property that fails on the first example
    above six errors.
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
    """R7: monotonicity in latency_ms — a slower station is never scored healthier."""
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
    """R4: above 6 errors the score carries no information about error volume.

    This is the saturation blind spot stated as an invariant rather than as a
    single example: from 6 errors upwards, a station reporting a handful of faults
    and a station failing thousands of sessions are indistinguishable to every
    consumer of this API. If someone later makes the penalty unbounded — which is
    the obvious fix — this property goes red and forces the conversation.
    """
    a = compute_hygiene_score(connectivity, latency_ms, error_count)
    b = compute_hygiene_score(connectivity, latency_ms, other_error_count)

    assert a == b


@pytest.mark.p0
@given(latency_ms=latency, error_count=errors)
def test_going_offline_costs_forty_points_give_or_take_the_rounding(
    latency_ms: float, error_count: int
) -> None:
    """R7 / R15: the offline penalty is independent of the other two terms — to within 0.01.

    I first wrote this as `online - offline == approx(40.0)` and Hypothesis
    falsified it in under a second. The reason is R15: `scoring.py:37` rounds the
    *final* score, and `round()` is half-even over binary floats, so the two
    scores can round in opposite directions. At `latency_ms=0.1` the difference is
    40.01; at `latency_ms=0.5` it is 39.99.

    The honest property is therefore "40 ± one rounding step". Widening the
    tolerance to something vague like `approx(40, abs=1)` would have hidden the
    finding instead of recording it, and would also stop catching a real
    off-by-one in the constant.
    """
    online = compute_hygiene_score("online", latency_ms, error_count)
    offline = compute_hygiene_score("offline", latency_ms, error_count)

    assert abs((online - offline) - 40.0) <= 0.01 + 1e-9


@pytest.mark.p0
@given(latency_ms=latency)
def test_an_online_station_with_no_errors_can_never_be_flagged(latency_ms: float) -> None:
    """R2: latency alone is never enough to flag a station, at any value.

    Best-case penalty from latency alone is 20 points, so the floor is 80. A
    charger that is reachable and error-free but takes ten seconds to answer —
    unusable in practice — is scored 80 and stays off the worklist. Stated as a
    property because the interesting part is the *universal* quantifier: there is
    no latency, anywhere in the domain, that rescues this.
    """
    score = compute_hygiene_score("online", latency_ms, error_count=0)

    assert score >= 80.0
    assert is_flagged(score) is False


@pytest.mark.p0
@given(connectivity=connectivity, latency_ms=latency, error_count=errors)
def test_flag_agrees_with_the_threshold_for_every_input(
    connectivity: str, latency_ms: float, error_count: int
) -> None:
    """R7: `flagged` is exactly `score < FLAGGING_THRESHOLD`, with no drift.

    The service computes the score and the flag in two separate calls
    (`reports.py:13-18`) and stores both. This is the property that keeps them
    from ever disagreeing — including at the boundary, which Hypothesis reaches
    on its own once it finds the offline/0/0 corner.
    """
    score = compute_hygiene_score(connectivity, latency_ms, error_count)

    assert is_flagged(score) == (score < FLAGGING_THRESHOLD)


@pytest.mark.p0
@given(
    connectivity=connectivity,
    latency_ms=latency,
    error_count=errors,
)
def test_scoring_is_pure(connectivity: str, latency_ms: float, error_count: int) -> None:
    """R7: identical inputs always produce identical outputs.

    Cheap, and it is the guard against the one change that would make every other
    test in this file lie: a scoring function that reaches for `datetime.now()`,
    a random jitter, or process-global state. Any of those turn the whole suite
    non-deterministic in a way that is very hard to diagnose from a flaky CI run.
    """
    first = compute_hygiene_score(connectivity, latency_ms, error_count)
    second = compute_hygiene_score(connectivity, latency_ms, error_count)

    assert first == second


@pytest.mark.p1
@given(
    latency_ms=latency,
    error_count=errors,
)
def test_flagging_requires_more_than_connectivity_loss_alone(
    latency_ms: float, error_count: int
) -> None:
    """R2, stated from the other side: what does it take to flag an offline station?

    An offline station starts at exactly 60.0, on the wrong side of a strict `<`,
    so connectivity loss alone is never enough — it must also report latency or
    errors. My first version of this assumed *any* non-zero latency was enough;
    Hypothesis falsified it with `latency_ms=1e-9`, which is R15: a penalty below
    0.005 rounds away and the station lands back on 60.0 exactly.

    So the true precondition is "a penalty that survives rounding to two
    decimals": at least one error, or at least 0.1 ms of latency.
    """
    assume(error_count > 0 or latency_ms >= 0.1)
    score = compute_hygiene_score("offline", latency_ms, error_count)

    assert score < 60.0
    assert is_flagged(score) is True


@pytest.mark.p1
@pytest.mark.parametrize(
    "latency_ms",
    [0.0, 0.01, 0.05, 0.09, 0.0999],
    ids=lambda v: f"latency-{v}ms",
)
def test_offline_station_with_sub_threshold_latency_rounds_back_to_unflagged(
    latency_ms: float,
) -> None:
    """R15: R2's blind spot is an interval, not a single point.

    Found by Hypothesis while falsifying the property above. Rounding to two
    decimals happens *after* the penalties are subtracted, so every offline
    station reporting under 0.1 ms of latency with no errors scores exactly 60.0
    and stays off the worklist — not just the pristine (0 ms, 0 errors) case.

    That matters because "offline with a near-zero latency reading" is precisely
    what a station reports when its uplink has dropped and the value is a stale
    or default zero. The blind spot is wider than the boundary test suggests, and
    it sits exactly at the dispatch decision.
    """
    score = compute_hygiene_score("offline", latency_ms, error_count=0)

    assert score == 60.0
    assert is_flagged(score) is False


@pytest.mark.p1
def test_rounding_step_can_decide_the_flag() -> None:
    """R15: at the boundary the last 0.01 is settled by binary float rounding.

    0.1 ms of latency on an offline station gives a true score of 59.995 — dead on
    the half-step. Python rounds half to even *over the binary representation*, so
    it lands on 59.99 and the station is flagged; the neighbouring value rounds the
    other way. Nothing about which chargers get a truck rolled should be decided
    by IEEE-754 tie-breaking, and this test is here so that statement is on the
    record with a reproducible example attached.
    """
    assert compute_hygiene_score("offline", 0.1, 0) == 59.99
    assert compute_hygiene_score("offline", 0.0999, 0) == 60.0
    # ... and the same tie one bracket up rounds the *other* way:
    assert compute_hygiene_score("online", 0.1, 0) == 100.0
