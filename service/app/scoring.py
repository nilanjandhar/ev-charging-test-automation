"""
Network hygiene score computation.

Score starts at 100 and is reduced by:
  - Offline connectivity:  -40 points
  - High error count:      -5 per error, capped at -30
  - High latency:          -(latency_ms / 20), capped at -20

A station is flagged when its score falls below FLAGGING_THRESHOLD.
"""

FLAGGING_THRESHOLD = 60.0

OFFLINE_PENALTY = 40.0
ERROR_PENALTY_PER = 5.0
ERROR_PENALTY_CAP = 30.0
LATENCY_DIVISOR = 20.0
LATENCY_PENALTY_CAP = 20.0


def compute_hygiene_score(
    connectivity_status: str,
    latency_ms: float,
    error_count: int,
) -> float:
    score = 100.0

    if connectivity_status == "offline":
        score -= OFFLINE_PENALTY

    error_penalty = min(error_count * ERROR_PENALTY_PER, ERROR_PENALTY_CAP)
    score -= error_penalty

    latency_penalty = min(latency_ms / LATENCY_DIVISOR, LATENCY_PENALTY_CAP)
    score -= latency_penalty

    return round(max(score, 0.0), 2)


def is_flagged(score: float) -> bool:
    return score < FLAGGING_THRESHOLD
