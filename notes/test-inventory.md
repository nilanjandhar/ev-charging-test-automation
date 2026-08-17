# Test inventory

44 test functions, 74 collected (parametrised ones expand).

Every test carries a `Why:` line in its docstring saying what goes unnoticed
without it; `make inventory` fails if one is missing, so a test that cannot be
justified cannot be added. Generated — do not edit by hand. Tier definitions
are in `TEST_STRATEGY.md`.

| Tier | Tests |
|---|---|
| **P0** | 20 |
| **P1** | 20 |
| **P2** | 4 |

## `tests/unit/test_scoring_boundaries.py`

3 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P0 | `test_score_at_each_threshold` | A change to any scoring constant, divisor or cap must go red here | Hand-computed from the published formula, so a change to any constant fails here rather than sliding through both sides |
| P0 | `test_flagging_boundary_is_exclusive` | The flag test is strict `<`, so 60.0 exactly is *not* flagged | One keystroke separates flagging every dead station from flagging none |
| P0 | `test_dead_station_reporting_clean_metrics_is_not_flagged` | An offline station with no errors and no latency scores exactly 60.0 | The highest-severity finding: pins it so a fix is a deliberate, reviewed change rather than an accident |

## `tests/unit/test_scoring_properties.py`

8 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P0 | `test_score_stays_inside_its_reachable_range` | No input can push the score outside [10, 100] | Asserts the reachable [10, 100], not the documented [0, 100] - the documented bound survives a doubled penalty |
| P0 | `test_more_errors_never_improves_the_score` | Monotonicity in error_count — non-increasing, not strictly decreasing | A station reporting more problems must never be scored healthier; the universal form catches what examples cannot |
| P0 | `test_more_latency_never_improves_the_score` | Monotonicity in latency_ms — a slower station is never scored healthier | Same guarantee for the other input, and the pair is what makes the score defensible to an operator |
| P1 | `test_score_is_blind_to_error_count_above_the_cap` | Above 6 errors the score carries no information about error volume | States the saturation blind spot as an invariant, so making the penalty unbounded forces the conversation |
| P0 | `test_going_offline_costs_forty_points_give_or_take_the_rounding` | The offline penalty is independent of the other two terms — to within 0.01 | Catches any refactor that couples the three penalties, e.g. skipping latency when offline |
| P0 | `test_an_online_station_with_no_errors_can_never_be_flagged` | Latency alone is never enough to flag a station, at any value | The universal quantifier is the point: no latency anywhere in the domain rescues this blind spot |
| P0 | `test_flag_agrees_with_the_threshold_for_every_input` | `flagged` is exactly `score < FLAGGING_THRESHOLD`, with no drift | Score and flag are computed and stored separately; this is what stops them drifting apart |
| P1 | `test_rounding_step_can_decide_the_flag` | At the boundary the last 0.01 is settled by binary float rounding | Puts on record that the last 0.01 of a truck-roll decision is settled by IEEE-754 tie-breaking |

## `tests/contract/test_openapi_conformance.py`

5 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P1 | `test_the_service_publishes_a_valid_openapi_document` | The schema itself must be well-formed 3.1 and cover every documented endpoint | The contract layer and any generated client both consume this document; a renamed path breaks every consumer |
| P1 | `test_every_documented_success_response_matches_its_schema` | One flow, every endpoint, each response validated against its own contract | Catches a hand-written `response_model` or a raw `JSONResponse` drifting from the published shape |
| P1 | `test_validation_errors_match_the_documented_error_schema` | The 422 envelope is part of the contract and clients parse it | Clients read `detail[].loc` to highlight a bad field; a custom exception handler would break them silently |
| P2 | `test_the_404_the_service_actually_returns_is_documented` | A status code the service returns must appear in the schema it publishes | The one contract failure a code-generated schema cannot catch by construction |
| P1 | `test_timestamps_conform_to_the_date_time_format_they_declare` | The service violates its own published `format: date-time` | Pins a live defect the structural checks miss, because JSON Schema treats `format` as an annotation |

## `tests/contract/test_schemathesis_fuzz.py`

1 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P1 | `test_operation_conforms_to_its_published_schema` | Every operation answers schema-valid inputs with schema-valid responses | Covers the inputs I did not think of; it found both the naive-datetime and the integer-overflow defects |

## `tests/api/test_cross_endpoint_consistency.py`

8 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P0 | `test_a_single_report_is_reflected_identically_by_every_endpoint` | Ingest -> status -> list -> metrics must describe the same station | Four endpoints recompute 'latest per station' independently; nothing else forces them to agree about one station |
| P0 | `test_flagged_stations_agree_across_list_worklist_and_metrics` | The poor-hygiene worklist is exactly the set of stations flagged elsewhere | An operator triages from the worklist and drills into the detail view; if those disagree the tool dispatches to healthy sites |
| P0 | `test_metrics_aggregate_only_the_latest_report_per_station` | Superseded reports must not leak into network totals | The only guard against an 'optimised' join aggregating all history into the dashboard's totals |
| P1 | `test_metrics_on_an_empty_network` | The zero case has to be representable, not a division by zero | A fresh deployment is the one time everyone is watching, and it is the only path that can divide by zero |
| P1 | `test_an_infinite_latency_report_erases_the_network_average_entirely` | One report of `Infinity` and the latency KPI silently becomes null | Pins a live defect: one report silently turns the network KPI into null, which no threshold alert can fire on |
| P0 | `test_a_retried_report_does_not_duplicate_the_station_in_the_listing` | `GET /stations` must list each known station exactly once | At-least-once delivery is normal for field telemetry; without this the defect is invisible until capacity numbers are wrong |
| P0 | `test_a_retried_report_does_not_inflate_network_metrics` | One station that reports twice is still one station | The expensive half of the same defect: SLA and capacity reporting run off these counts |
| P1 | `test_a_retried_report_leaves_the_station_detail_view_correct` | `/stations/{id}/status` is idempotent | Shows the asymmetry that lets the duplicate defect survive a manual check - one endpoint hides what two others double |

## `tests/api/test_ingest_validation.py`

7 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P1 | `test_invalid_field_is_rejected_with_a_machine_readable_error` | A bad field must produce 422 naming that field — not a 500, not a silent accept | Clients branch on the error `type` and `loc`; nothing else pins that contract, and it is framework default nobody chose |
| P1 | `test_every_field_is_required` | No field has a server-side default; omitting any one is a 422, not a partial record | One case per field, because a `Field(...)` that quietly gains a default turns bad rows silent rather than rejected |
| P0 | `test_unknown_fields_are_ignored_and_cannot_override_the_computed_score` | A client cannot inject its own hygiene score. This is the one that matters | `hygiene_score` and `flagged` are real columns; a client that could set them would defeat the entire service |
| P1 | `test_malformed_bodies_are_422_not_500` | Junk on the wire must never reach the database layer or produce a stack trace | Junk on the wire must not reach the database layer or produce a stack trace |
| P1 | `test_unknown_station_is_a_clean_404` | A station nobody has reported for must 404 with a usable message, not an empty 200 | 'Never reported' and 'reported fine' must not look the same to a monitoring script |
| P1 | `test_an_enormous_error_count_is_rejected_rather_than_crashing_ingest` | Schema-valid input that reaches the storage layer and blows up there | Pins an unhandled 500 on an unauthenticated endpoint - validation that the storage layer rejects is validation in the wrong place |
| P1 | `test_the_int64_boundary_below_the_crash_is_accepted` | The other side of that boundary, so the xfail above cannot drift | Stops the fix for that crash from over-correcting into rejecting legitimate counts |

## `tests/api/test_recency_semantics.py`

4 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P0 | `test_status_reflects_the_newest_timestamp_not_the_newest_arrival` | Arrival order must not decide station state | Buffered stations flush out of order routinely; this property is what makes the whole ingest path safe to retry |
| P0 | `test_a_future_dated_report_permanently_masks_every_later_report` | One bad clock and the station is never heard from again | Reproduces a live defect: one bad clock freezes a station green forever, and the fix is a product decision |
| P1 | `test_utc_offsets_are_dropped_rather_than_normalised` | Two reports of the same instant are not treated as the same instant | The suite's builders always emit UTC, so this is the only place a cross- timezone fleet's ordering bug is visible |
| P1 | `test_reports_that_tie_on_timestamp_do_not_crash_the_detail_view` | A tie has to resolve to *something* deterministic per endpoint | Asserts a tie resolves to one valid row rather than a specific winner, which SQLite and PostgreSQL would not agree on |

## `tests/e2e/test_live_service.py`

4 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P0 | `test_health_endpoint_answers_over_real_http` | The readiness signal CI polls has to be exactly what CI expects | Every e2e and perf job's readiness poll waits on this exact body; a change would hang CI instead of failing it |
| P0 | `test_a_station_journey_over_the_wire` | The whole operator journey against the real deployment, in deltas | The only test where the latest-per-station join runs against PostgreSQL rather than SQLite |
| P1 | `test_error_responses_serialise_correctly_over_http` | 422 and 404 survive real serialisation, with real content types | In-process tests get a Python object back; only here does the error body have to survive real JSON encoding |
| P2 | `test_the_deployment_serves_its_documentation_and_dashboard` | The static mount and the docs routes only exist in a real deployment | The static mount is conditional on a directory existing in the image - invisible to every in-process test |

## `tests/perf/test_concurrent_writes.py`

2 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P0 | `test_concurrent_reports_for_one_station_all_land` | N simultaneous writers, N stored reports, and the final state is one of them | The write path is append-only today; this is what goes red if someone 'optimises' ingest into an upsert |
| P1 | `test_in_process_client_agrees_with_the_wire_on_a_single_report` | A control for the whole e2e layer: does in-process testing miss anything here? | The control for the whole in-process/live split - if it fails, the fast layers are lying |

## `tests/perf/test_latency_smoke.py`

1 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P2 | `test_read_and_write_latency_against_a_configured_budget` | Every documented endpoint answers within the configured p95 budget | An order-of-magnitude tripwire and a trend printed into CI logs; never a gate, for reasons in TEST_STRATEGY.md |

## `tests/ui/test_dashboard_smoke.py`

1 tests.

| Tier | Test | Covers | Why it must exist |
|---|---|---|---|
| P2 | `test_dashboard_renders_an_ingested_station` | A station ingested via the API appears on the dashboard with its real status | The dashboard reads six fields across three endpoints; an API change can stay green and still blank the operator's screen |
